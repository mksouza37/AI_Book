import os
from typing import Dict, Any, Optional
from dotenv import load_dotenv
from crewai import Agent, Crew, Process, Task
from crewai.tools import BaseTool
from google.oauth2 import service_account
from googleapiclient.discovery import build
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from pydantic import Field, BaseModel
import json
from datetime import datetime, timedelta
import logging
import time
from flask import Flask, request, Response

# Initialize Flask app
app = Flask(__name__)

# Load environment variables
load_dotenv()

# Configuration
PORT = int(os.environ.get('PORT', 10000))
PRICE_LIST_PDF_URL = "https://www.dropbox.com/scl/fi/5ppj1wvzj6lo49lz3kjw4/services_pricelist_1.pdf?rlkey=a8756on4fqhqpnfmbhfo07mhj&st=263kad9k&dl=1"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('scheduler.log')
    ]
)
logger = logging.getLogger(__name__)


# ======================
# Output Model
# ======================
class EventDetails(BaseModel):
    action: str
    time_iso: str
    summary: Optional[str] = None
    duration_hours: Optional[float] = None


# ======================
# Google Calendar Tool
# ======================
class GoogleCalendarTool(BaseTool):
    """Ferramenta para criar/cancelar eventos no Google Calendar"""
    name: str = "Ferramenta do Google Calendar"
    description: str = "Cria ou cancela eventos no Google Calendar"
    calendar_id: str = Field(default=os.getenv("GOOGLE_CALENDAR_ID"))
    service: Any = Field(default=None, exclude=True)
    timezone: str = "America/Sao_Paulo"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._setup_service()

    def _setup_service(self):
        """Configura o serviço do Google Calendar"""
        try:
            # Using credentials.json file for local development
            if os.path.exists("credentials.json"):
                creds = service_account.Credentials.from_service_account_file(
                    "credentials.json",
                    scopes=['https://www.googleapis.com/auth/calendar']
                )
            # Using environment variable for Render deployment
            elif os.getenv("GOOGLE_CREDENTIALS"):  # CHANGED THIS LINE
                service_account_info = json.loads(os.getenv("GOOGLE_CREDENTIALS"))  # CHANGED THIS LINE
                creds = service_account.Credentials.from_service_account_info(
                    service_account_info,
                    scopes=['https://www.googleapis.com/auth/calendar']
                )
            else:
                raise ValueError("No Google Calendar credentials found")

            self.service = build('calendar', 'v3', credentials=creds)
            logger.info("Google Calendar service initialized successfully")
        except Exception as e:
            logger.error(f"Error setting up Google Calendar service: {str(e)}")
            raise
    def _format_date_pt(self, time_iso: str) -> tuple:
        """Helper to format date in Portuguese"""
        data_obj = datetime.fromisoformat(time_iso)
        dia = data_obj.strftime("%d")
        mes = data_obj.strftime("%B")

        meses_pt = {
            'January': 'Janeiro', 'February': 'Fevereiro', 'March': 'Março',
            'April': 'Abril', 'May': 'Maio', 'June': 'Junho',
            'July': 'Julho', 'August': 'Agosto', 'September': 'Setembro',
            'October': 'Outubro', 'November': 'Novembro', 'December': 'Dezembro'
        }

        return (dia, meses_pt.get(mes, mes), data_obj.strftime("%H:%M"))

    def _format_date_pt_short(self, date_obj: datetime) -> str:
        """Helper to format date in Portuguese (short version)"""
        meses_pt = {
            1: 'Jan', 2: 'Fev', 3: 'Mar',
            4: 'Abr', 5: 'Mai', 6: 'Jun',
            7: 'Jul', 8: 'Ago', 9: 'Set',
            10: 'Out', 11: 'Nov', 12: 'Dez'
        }
        return f"{date_obj.day} de {meses_pt[date_obj.month]}"

    def _get_free_slots(self, date_str: str) -> Dict[str, Any]:
        """Get free time slots for a specific date (8am-7pm, no Sundays)"""
        try:
            logger.info(f"Getting free slots for date: {date_str}")

            # Parse the input date
            today = datetime.now().astimezone()
            input_parts = date_str.split('/')

            if len(input_parts) == 1:  # Only day provided
                day = int(input_parts[0])
                target_date = today.replace(day=day)
                if target_date < today:
                    target_date = target_date.replace(month=today.month + 1)
                    if today.month == 12:
                        target_date = target_date.replace(year=today.year + 1, month=1)
            else:  # Day and month provided
                day = int(input_parts[0])
                month = int(input_parts[1])
                target_date = today.replace(day=day, month=month)
                if target_date < today:
                    target_date = target_date.replace(year=today.year + 1)

            # Check if it's Sunday
            if target_date.weekday() == 6:
                return {
                    'date': self._format_date_pt_short(target_date),
                    'free_slots': [],
                    'is_sunday': True,
                    'target_date': target_date.date().isoformat()
                }

            # Set time bounds (8am to 7pm)
            start_time = target_date.replace(hour=8, minute=0, second=0, microsecond=0)
            end_time = target_date.replace(hour=19, minute=0, second=0, microsecond=0)

            # Get existing events
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=start_time.isoformat(),
                timeMax=end_time.isoformat(),
                singleEvents=True,
                orderBy='startTime',
                timeZone=self.timezone
            ).execute()

            events = events_result.get('items', [])

            # Generate all possible slots (every hour from 8am to 7pm)
            all_slots = []
            current_time = start_time
            while current_time < end_time:
                all_slots.append({
                    'start': current_time,
                    'end': current_time + timedelta(hours=1)
                })
                current_time += timedelta(hours=1)

            # Mark busy slots
            for event in events:
                event_start = datetime.fromisoformat(event['start']['dateTime'])
                event_end = datetime.fromisoformat(event['end']['dateTime'])

                for slot in all_slots:
                    if not (event_end <= slot['start'] or event_start >= slot['end']):
                        slot['busy'] = True

            # Format free slots
            free_slots = []
            for slot in all_slots:
                if not slot.get('busy'):
                    free_slots.append({
                        'start': slot['start'].strftime('%H:%M'),
                        'end': slot['end'].strftime('%H:%M')
                    })

            return {
                'date': self._format_date_pt_short(target_date),
                'free_slots': free_slots,
                'is_sunday': False,
                'target_date': target_date.date().isoformat()
            }

        except Exception as e:
            logger.error(f"Error getting free slots: {str(e)}", exc_info=True)
            raise

    def _run(self, event_details: Dict[str, Any]) -> str:
        try:
            logger.info(f"Processing event: {event_details}")
            action = event_details.get('action', 'criar')
            dia, mes_pt, hora = self._format_date_pt(event_details['time_iso'])

            if action == "cancelar":
                logger.info(f"Processing cancellation for: {event_details}")
                event_id = self._encontrar_evento_por_hora(
                    event_details['time_iso'],
                    event_details.get('summary')
                )
                logger.info(f"Found event ID: {event_id}")

                if not event_id:
                    logger.warning("No event found to cancel")
                    return "❌ Reunião não encontrada"

                logger.info(f"Canceling event ID: {event_id}")
                self.service.events().delete(
                    calendarId=self.calendar_id,
                    eventId=event_id
                ).execute()

                confirmacao = (
                    "🗑️ *Cancelamento Confirmado!*\n\n"
                    f"📅 *Data Cancelada:* {dia} de {mes_pt}\n"
                    f"⏰ *Horário:* {hora}\n\n"
                    f"🗒️ *Detalhes:* {event_details.get('summary', 'Reunião com Cláudia')}\n\n"
                    "📱 Lembrete: Este horário está agora disponível para novos agendamentos.\n"
                    "🔄 *Precisa reagendar?* Me avise!"
                )
                logger.info("Event canceled successfully")
                return confirmacao

            elif action == "criar":
                logger.info(f"Processing event creation: {event_details}")
                start_time = datetime.fromisoformat(event_details['time_iso'])
                duration = event_details.get('duration_hours', 1)
                end_time = start_time + timedelta(hours=duration)

                event = {
                    'summary': event_details.get('summary', 'Reunião Automática'),
                    'start': {
                        'dateTime': start_time.isoformat(),
                        'timeZone': self.timezone,
                    },
                    'end': {
                        'dateTime': end_time.isoformat(),
                        'timeZone': self.timezone,
                    },
                }
                logger.info(f"Event to be created: {event}")

                created_event = self.service.events().insert(
                    calendarId=self.calendar_id,
                    body=event
                ).execute()

                confirmacao = (
                    "✅ *Agendamento Confirmado!*\n\n"
                    f"📅 *Data:* {dia} de {mes_pt}\n"
                    f"⏰ *Horário:* {hora}\n\n"
                    f"🗒️ *Detalhes:* {event_details.get('summary', 'Reunião com Cláudia')}\n\n"
                    "📱 *Lembrete:* Você receberá uma notificação 1 hora antes.\n"
                    "🔄 *Precisa reagendar?* Me avise com 24h de antecedência."
                )
                logger.info("Event created successfully")
                return confirmacao

        except Exception as e:
            logger.error(f"Error processing event: {str(e)}", exc_info=True)
            return f"❌ Erro: {str(e)}"

    def _encontrar_evento_por_hora(self, time_iso: str, summary: str) -> str:
        """Encontra ID do evento por horário e título"""
        try:
            logger.info(f"Searching for event: {time_iso} - {summary}")
            start_time = datetime.fromisoformat(time_iso).astimezone()
            time_min = (start_time - timedelta(minutes=30)).isoformat()
            time_max = (start_time + timedelta(minutes=30)).isoformat()

            logger.info(f"Querying calendar between {time_min} and {time_max}")
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                timeZone=self.timezone
            ).execute()

            events = events_result.get('items', [])
            logger.info(f"Found events: {len(events)}")

            if not events:
                logger.info("No events found in the period")
                return None

            if summary:
                logger.info(f"Searching for title containing: {summary}")
                for event in events:
                    if summary.lower() in event.get('summary', '').lower():
                        logger.info(f"Found event by title: {event['id']}")
                        return event['id']

            logger.info(f"Returning first event in period: {events[0]['id']}")
            return events[0]['id']
        except Exception as e:
            logger.error(f"Error finding event: {str(e)}", exc_info=True)
            return None


# ======================
# WhatsApp Functions
# ======================
def enviar_saudacao_inicial(numero: str):
    """Envia mensagem de apresentação inicial"""
    mensagem = (
        "👋 *Olá!* Sou a IAIÁ, o braço direito da Cláudia.* 🤖✨\n\n"
        "📅 *Posso agendar seu horário com ela!* É simples:\n"
        "   - Me diga o *dia* e *horário* que deseja\n"
        "   - Ex: *\"Quero agendar dia 25/07 às 15h\"*\n\n"
        "💬 *Precisa de outra coisa?*\n"
        "   - Me envie sua solicitação\n"
        "   - Ex: *\"Gostaria de saber sobre valores.\"*\n"
        "   - Eu repasso pra ela e *ela te responde pessoalmente* 💛\n\n"
        "⏳ *Retorno garantido ainda hoje!*\n"
        "📲 *Vamos começar?*"
    )
    enviar_mensagem_whatsapp(mensagem, numero)


def enviar_mensagem_whatsapp(mensagem: str, numero: str):
    """Envia mensagem via WhatsApp (Twilio)"""
    try:
        client = Client(
            os.getenv('TWILIO_ACCOUNT_SID'),
            os.getenv('TWILIO_AUTH_TOKEN')
        )
        client.messages.create(
            body=mensagem,
            from_=os.getenv('TWILIO_WHATSAPP_NUMBER'),
            to=f"whatsapp:{numero.lstrip('+')}"
        )
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {str(e)}")


# ======================
# CrewAI Setup
# ======================
calendar_tool = GoogleCalendarTool()

agente_agendamento = Agent(
    role="Assistente de Agendamento WhatsApp",
    goal="Processar mensagens em português e gerar JSON exato para agendamentos",
    backstory=(
        "Especialista em converter mensagens em português para um formato JSON estruturado "
        "com os campos: action, time_iso, summary e duration_hours. "
        "Sempre usa o formato ISO 8601 com timezone para datas."
    ),
    tools=[calendar_tool],
    verbose=True,
    max_iter=5,
    memory=True,
    allow_delegation=False,
    language="pt-br"
)


# ======================
# Message Processing
# ======================
def processar_mensagem(mensagem: str, numero: str, primeira_vez: bool = True):
    """Processa mensagens em português para agendar/cancelar ou encaminhar"""
    try:
        if primeira_vez:
            enviar_saudacao_inicial(numero)
            time.sleep(2)

        logger.info(f"Processing message: {mensagem} for {numero}")

        # Check message type
        palavras_agendamento = ["agendar", "marcar", "horário", "hora", "reunião", "consulta", "visita"]
        palavras_cancelamento = ["cancelar", "desmarcar", "remover", "excluir"]
        palavras_horarios = ["horários", "horarios", "disponíveis", "disponiveis", "vagas", "abertos"]
        palavras_precos = ["preços", "precos", "valores", "tabela", "serviços", "servicos", "menu", "cardápio",
                           "cardapio"]

        mensagem_lower = mensagem.lower()
        eh_agendamento = any(palavra in mensagem_lower for palavra in palavras_agendamento)
        eh_cancelamento = any(palavra in mensagem_lower for palavra in palavras_cancelamento)
        eh_horarios = any(palavra in mensagem_lower for palavra in palavras_horarios)
        eh_precos = any(palavra in mensagem_lower for palavra in palavras_precos)

        # Handle price list request
        if eh_precos:
            try:
                client = Client(
                    os.getenv('TWILIO_ACCOUNT_SID'),
                    os.getenv('TWILIO_AUTH_TOKEN')
                )
                client.messages.create(
                    media_url=[PRICE_LIST_PDF_URL],
                    from_=os.getenv('TWILIO_WHATSAPP_NUMBER'),
                    to=f"whatsapp:{numero.lstrip('+')}",
                    body=(
                        "📋 *Aqui está nossa lista de serviços/preços!*\n\n"
                        "🔹 *Como agendar:*\n"
                        "Responda com: *\"Quero agendar para [dia] às [hora]\"*\n\n"
                        "📌 *Exemplo:*\n"
                        "*\"Quero agendar para sexta às 15h\"*"
                    )
                )
                return
            except Exception as e:
                error_msg = "❌ *Não consegui enviar o PDF no momento*"
                enviar_mensagem_whatsapp(error_msg, numero)
                return

        # Handle free slots request
        if eh_horarios:
            import re
            date_match = re.search(r'(\d{1,2})(?:\s*\/\s*(\d{1,2}))?', mensagem)
            if date_match:
                day = date_match.group(1)
                month = date_match.group(2) if date_match.group(2) else None
                date_str = f"{day}/{month}" if month else day

                try:
                    free_slots = calendar_tool._get_free_slots(date_str)

                    if free_slots.get('is_sunday'):
                        resposta = (
                            f"📅 *Domingo - {free_slots['date']}*\n\n"
                            "⛔ *Não atendemos aos domingos.*\n\n"
                            "Por favor, escolha outro dia da semana."
                        )
                    elif not free_slots['free_slots']:
                        resposta = (
                            f"📅 *Horários para {free_slots['date']}*\n\n"
                            "❌ *Não há horários disponíveis neste dia.*"
                        )
                    else:
                        slots_text = "\n".join(
                            f"🕒 *{slot['start']} - {slot['end']}*"
                            for slot in free_slots['free_slots']
                        )

                        resposta = (
                            f"📅 *Horários Disponíveis - {free_slots['date']}*\n\n"
                            f"{slots_text}\n\n"
                            "🔹 *Como agendar:*\n"
                            f"Responda com: *\"Quero o horário das XXh do dia {free_slots['date']}\"*"
                        )

                    enviar_mensagem_whatsapp(resposta, numero)
                    return

                except Exception as e:
                    error_msg = "❌ *Não consegui verificar os horários*"
                    enviar_mensagem_whatsapp(error_msg, numero)
                    return

        if not (eh_agendamento or eh_cancelamento or eh_horarios or eh_precos):
            # Forward message to Claudia
            mensagem_encaminhada = (
                "📩 *Novo Pedido de Cliente*\n\n"
                f"*Mensagem:* {mensagem}\n"
                f"*Número:* {numero}\n"
                f"*Data/Hora:* {datetime.now().strftime('%d/%m/%Y %H:%M')}"
            )
            enviar_mensagem_whatsapp(mensagem_encaminhada, "+5511981583453")

            resposta_cliente = (
                "📨 *Mensagem Encaminhada!*\n\n"
                "Sua solicitação foi enviada diretamente para a Cláudia.\n"
                "Ela responderá pessoalmente em breve!"
            )
            enviar_mensagem_whatsapp(resposta_cliente, numero)
            return

        # Process scheduling/cancellation
        data_atual = datetime.now().strftime("%Y-%m-%d")

        exemplos = """
        EXEMPLOS VÁLIDOS:
        - AGENDAR: "marcar reunião amanhã às 14h sobre o projeto X"
        - CANCELAR: "cancelar a reunião de quinta-feira às 10h"
        """

        tarefa = Task(
            description=(
                f"Data atual: {data_atual}\n"
                f"Mensagem recebida: '{mensagem}'\n"
                f"{exemplos}\n"
                "RETORNE APENAS UM OBJETO JSON VÁLIDO COM ESTES CAMPOS:\n"
                "{\n"
                '  "action": "criar" ou "cancelar",\n'
                '  "time_iso": "Data/hora ISO com timezone",\n'
                '  "summary": "Título da reunião",\n'
                '  "duration_hours": 1\n'
                "}"
            ),
            agent=agente_agendamento,
            expected_output="APENAS o JSON válido sem nenhum texto adicional",
            output_json=EventDetails
        )

        crew = Crew(
            agents=[agente_agendamento],
            tasks=[tarefa],
            process=Process.sequential,
            verbose=True
        )

        resultado = crew.kickoff()
        logger.info(f"Crew result: {resultado}")

        try:
            if isinstance(resultado, dict):
                event_data = resultado
            else:
                output_str = str(resultado).strip()
                output_str = output_str.replace("'", '"').replace("None", "null")
                event_data = json.loads(output_str)

            # Validate required fields
            required_fields = ['action', 'time_iso']
            if not all(field in event_data for field in required_fields):
                raise ValueError("Missing required fields")

            event_details = {
                "action": str(event_data['action']),
                "time_iso": str(event_data['time_iso'])
            }

            if 'summary' in event_data and event_data['summary'] is not None:
                event_details['summary'] = str(event_data['summary'])

            if event_data['action'] == 'criar':
                event_details['duration_hours'] = float(event_data.get('duration_hours', 1))

            # Execute the action
            resultado_calendario = calendar_tool.run(event_details)
            enviar_mensagem_whatsapp(resultado_calendario, numero)

        except Exception as e:
            error_msg = f"❌ Erro: {str(e)}"
            enviar_mensagem_whatsapp(error_msg, numero)

    except Exception as e:
        logger.error(f"General processing error: {str(e)}", exc_info=True)
        enviar_mensagem_whatsapp("❌ Ocorreu um erro ao processar sua mensagem.", numero)


# ======================
# Flask Routes
# ======================
@app.route('/')
def health_check():
    """Health check endpoint for Render"""
    return "IAIÁ WhatsApp Bot is running!", 200


@app.route('/webhook', methods=['POST'])
def webhook():
    """Twilio webhook handler"""
    try:
        incoming_msg = request.values.get('Body', '').strip()
        sender = request.values.get('From', '').strip()

        if not incoming_msg or not sender:
            return Response("Invalid request", status=400)

        logger.info(f"Received message from {sender}: {incoming_msg}")

        # Process the message
        processar_mensagem(incoming_msg, sender)

        # Return empty TwiML response
        resp = MessagingResponse()
        return Response(str(resp), 200, {'Content-Type': 'text/xml'})

    except Exception as e:
        logger.error(f"Error in webhook: {str(e)}", exc_info=True)
        return Response("Server Error", status=500)


# ======================
# Main Entry Point
# ======================
if __name__ == "__main__":
    try:
        # Initialize services
        calendar_tool._setup_service()
        logger.info("Google Calendar service initialized")

        test_client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
        logger.info("Twilio client initialized")

        logger.info(f"Starting server on port {PORT}")
        app.run(host='0.0.0.0', port=PORT, debug=False)

    except Exception as e:
        logger.error(f"Failed to  initialize application: {str(e)}", exc_info=True)