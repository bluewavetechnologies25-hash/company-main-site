import json
import logging
import re
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.core.mail import send_mail
from django.conf import settings
from groq import Groq

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Maps, a warm, polished, and highly capable AI assistant for Bluewave Technologies. You are not a generic chatbot; you act like a helpful human concierge for potential clients and visitors.

About Bluewave Technologies:
- Services: Enterprise Software Development, AI Solutions & Machine Learning, Mobile Application Development, Business Automation, Cloud Infrastructure, and Digital Transformation
- Process: Discovery & Planning → Design & Architecture → Development → Testing & QA → Deployment → Ongoing Support
- Team: Experienced engineers and designers based locally but delivering to global standards
- Tagline: "Local Technology. Global Standards."
- Stats: 98.7% accuracy, 12,483 active users, 99.98% uptime

Your goals:
1. Answer questions clearly and naturally about Bluewave Technologies, its services, expertise, process, and value
2. Help visitors feel understood and guided, like a thoughtful human assistant
3. Ask smart follow-up questions when needed to clarify the visitor's goals
4. Help collect a quote request when the visitor seems ready to discuss a project
5. Be concise, warm, professional, and practical

Personality:
- Friendly and confident, but not overly chatty
- Helpful, proactive, and solution-oriented
- Comfortable sounding like a real consultant rather than a robotic assistant
- Can gently guide the conversation toward project details, timeline, budget, and contact info when relevant

When collecting a quote request:
- Ask about the project or business problem
- Ask for the budget range if the user is comfortable sharing it
- Ask for the preferred timeline
- Collect at least one contact method: email OR WhatsApp number
- Ask for the name
- Once you have: name + (email or whatsapp) + project details, respond with a JSON block at the END of your message in this exact format:
  QUOTE_READY:{"name":"...","email":"...","whatsapp":"...","project_details":"...","budget":"...","timeline":"..."}

Important rules:
- Do not include the QUOTE_READY block until all required fields are present
- If the visitor expresses interest in a project, keep the conversation moving and ask for the missing details one step at a time
- Never stop after a price or project description without asking for the visitor's name and at least one contact method
- Keep responses concise, polished, and human-like
- Avoid emojis
- If the user is just browsing, answer helpfully and keep the conversation moving naturally
- If the user is asking about services, explain them clearly and offer next steps"""


def _extract_lead_context(text):
    text = text or ""
    normalized = text.strip()

    budget = None
    timeline = None
    email = None
    whatsapp = None
    name = None

    budget_match = re.search(r"(?i)\b(?:budget|price|cost)\b[^\d$]{0,8}\$?\s*(\d{2,6})(?:\s*(?:usd|us dollars?|dollars?))?", normalized)
    if budget_match:
        budget = f"${budget_match.group(1)}"

    timeline_match = re.search(r"(?i)\b(?:timeline|when|by|within)\b[^\d]{0,6}(\d+\s*(?:days?|weeks?|months?))", normalized)
    if timeline_match:
        timeline = timeline_match.group(1)

    email_match = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", normalized, re.I)
    if email_match:
        email = email_match.group(0)

    whatsapp_match = re.search(
        r"(?i)(?:whatsapp|wa|chat)\s*[:#-]?\s*(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}",
        normalized,
    )
    if whatsapp_match:
        whatsapp = whatsapp_match.group(0)
        whatsapp = re.sub(r"(?i)^\s*(?:whatsapp|wa|chat)\s*[:#-]?\s*", "", whatsapp).strip()
    else:
        fallback = re.search(r"(?:(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3}[-.\s]?\d{4})", normalized)
        if fallback:
            whatsapp = fallback.group(0)

    name_match = re.search(r"(?i)\bmy name is\s+([a-z][a-z .'-]+)\b", normalized)
    if name_match:
        name = name_match.group(1).strip()
        name = re.sub(r"\s+(?:email|whatsapp)\b.*$", "", name, flags=re.I)
        name = re.sub(r"[.\s]+$", "", name).strip()

    return {
        'project_details': normalized or '',
        'budget': budget,
        'timeline': timeline,
        'email': email,
        'whatsapp': whatsapp,
        'name': name,
    }


def _has_complete_quote(data):
    if not isinstance(data, dict):
        return False
    name = (data.get('name') or '').strip()
    project_details = (data.get('project_details') or '').strip()
    email = (data.get('email') or '').strip()
    whatsapp = (data.get('whatsapp') or '').strip()
    return bool(name and project_details and (email or whatsapp))


def _build_follow_up_reply(reply, user_message):
    context = _extract_lead_context(user_message)
    has_contact = bool(context['email'] or context['whatsapp'] or context['name'])
    if has_contact or not context['project_details']:
        return None

    budget_text = f" with a budget of {context['budget']}" if context['budget'] else ""
    is_project_interest = bool(context['project_details']) and (context['budget'] or 'website' in context['project_details'].lower() or 'app' in context['project_details'].lower() or 'project' in context['project_details'].lower())
    if is_project_interest:
        follow_up = (
            f"{reply}\n\n"
            f"Thanks — that gives me a good sense of the project{budget_text}. "
            "To keep things moving, could you send your WhatsApp number so our team can follow up quickly?"
        )
    else:
        follow_up = (
            f"{reply}\n\n"
            f"Thanks — that gives me a good sense of the project{budget_text}. "
            "To help the team follow up properly, could you share your name and either your email or WhatsApp number?"
        )
    return follow_up


def home(request):
    return render(request, 'core/index.html')


@csrf_exempt
@require_POST
def chat_api(request):
    try:
        data = json.loads(request.body)
        user_message = data.get('message', '').strip()
        history = data.get('history', [])

        if not user_message:
            return JsonResponse({'error': 'No message provided'}, status=400)

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for msg in history[-20:]:
            if msg.get('role') in ('user', 'assistant') and msg.get('content'):
                messages.append({"role": msg['role'], "content": msg['content']})
        messages.append({"role": "user", "content": user_message})

        client = Groq(api_key=settings.GROQ_API_KEY)
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )

        reply = completion.choices[0].message.content

        display_reply = reply
        quote_data = None
        if 'QUOTE_READY:' in reply:
            try:
                json_start = reply.index('QUOTE_READY:') + len('QUOTE_READY:')
                json_str = reply[json_start:].strip()
                brace_end = json_str.index('}') + 1
                parsed = json.loads(json_str[:brace_end])
                if _has_complete_quote(parsed):
                    quote_data = parsed
                    display_reply = reply[:reply.index('QUOTE_READY:')].strip()
                    _save_and_notify_quote(quote_data)
                else:
                    display_reply = reply[:reply.index('QUOTE_READY:')].strip() or reply
                    follow_up = _build_follow_up_reply(display_reply, user_message)
                    if follow_up:
                        display_reply = follow_up
            except (ValueError, json.JSONDecodeError):
                display_reply = reply
                follow_up = _build_follow_up_reply(display_reply, user_message)
                if follow_up:
                    display_reply = follow_up
        else:
            follow_up = _build_follow_up_reply(display_reply, user_message)
            if follow_up:
                display_reply = follow_up

        return JsonResponse({
            'reply': display_reply,
            'quote_captured': quote_data is not None,
        })

    except Exception as e:
        logger.error(f"Chat API error: {e}")
        return JsonResponse({'error': 'Something went wrong. Please try again.'}, status=500)


@csrf_exempt
@require_POST
def submit_quote(request):
    try:
        data = json.loads(request.body)
        from .models import QuoteRequest
        quote = QuoteRequest.objects.create(
            name=data.get('name', ''),
            email=data.get('email', ''),
            whatsapp=data.get('whatsapp', ''),
            project_details=data.get('project_details', ''),
            budget=data.get('budget', ''),
            timeline=data.get('timeline', ''),
        )
        _send_quote_email(quote)
        quote.notified = True
        quote.save()
        return JsonResponse({'success': True})
    except Exception as e:
        logger.error(f"Quote submit error: {e}")
        return JsonResponse({'error': 'Failed to submit quote'}, status=500)


@csrf_exempt
@require_POST
def contact_message(request):
    try:
        data = json.loads(request.body)
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        subject = data.get('subject', '').strip() or 'Website Contact Form'
        message = data.get('message', '').strip()

        if not name or not email or not message:
            return JsonResponse({'error': 'Name, email and message are required.'}, status=400)

        from .models import ContactMessage
        msg = ContactMessage.objects.create(
            name=name,
            email=email,
            subject=subject,
            message=message,
        )

        if settings.COMPANY_EMAIL:
            body = f"""New contact message received via Bluewave website.

From:    {name}
Email:   {email}
Subject: {subject}

Message:
{message}

---
Reply directly to this email to respond to {name}.
"""
            try:
                send_mail(
                    f"[Bluewave] {subject}",
                    body,
                    settings.DEFAULT_FROM_EMAIL,
                    [settings.COMPANY_EMAIL],
                    fail_silently=False,
                    reply_to=[email],
                )
                msg.notified = True
                msg.save()
            except Exception as e:
                logger.error(f"Contact email send error: {e}")

        return JsonResponse({'success': True})

    except Exception as e:
        logger.error(f"Contact message error: {e}")
        return JsonResponse({'error': 'Failed to send message. Please try again.'}, status=500)


def _save_and_notify_quote(data):
    try:
        from .models import QuoteRequest
        quote = QuoteRequest.objects.create(
            name=data.get('name', ''),
            email=data.get('email', ''),
            whatsapp=data.get('whatsapp', ''),
            project_details=data.get('project_details', ''),
            budget=data.get('budget', ''),
            timeline=data.get('timeline', ''),
        )
        _send_quote_email(quote)
        quote.notified = True
        quote.save()
    except Exception as e:
        logger.error(f"Quote save/notify error: {e}")


def _send_quote_email(quote):
    if not settings.COMPANY_EMAIL:
        return
    subject = f"New Quote Request from {quote.name}"
    body = f"""New quote request received via Bluewave website chatbot.

Client Details:
  Name:      {quote.name}
  Email:     {quote.email or 'Not provided'}
  WhatsApp:  {quote.whatsapp or 'Not provided'}

Project Details:
{quote.project_details}

Budget:    {quote.budget or 'Not specified'}
Timeline:  {quote.timeline or 'Not specified'}

Submitted: {quote.created_at.strftime('%Y-%m-%d %H:%M UTC')}

---
Reply directly to this email or contact the client via WhatsApp.
"""
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [settings.COMPANY_EMAIL],
            fail_silently=False,
        )
    except Exception as e:
        logger.error(f"Email send error: {e}")
