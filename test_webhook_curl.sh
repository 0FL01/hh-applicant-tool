#!/bin/bash
# Webhook test для 3 уровня (llm-tg-assistant)
# Замените значения в квадратных скобках на реальные данные

WEBHOOK_URL="http://localhost:8081/webhooks/hh/recruiter-contact-offer"
IDEMPOTENCY_KEY="recruiter_contact_offer:1234567890:msg_abc123def456"
SECRET="your-webhook-secret-here"  # Замените на ваш секрет

cat > /tmp/webhook_payload.json << 'EOF'
{
  "event_type": "recruiter_contact_offer",
  "idempotency_key": "$IDEMPOTENCY_KEY",
  "created_at": "2026-03-13T14:32:15.432Z",
  "run_id": "uuid-from-agent-run",
  "classifier": {
    "category": "recruiter_contact_offer",
    "action": "skip",
    "reason": "найден прямой контакт рекрутера в Telegram",
    "confidence": 0.92
  },
  "candidate": {
    "first_name": "Андрей",
    "last_name": "Башкин",
    "resume_id": "157834926",
    "resume_title": "Backend Developer (Go, Rust)"
  },
  "negotiation": {
    "id": 1234567890,
    "chat_id": "5678901234@hh.ru",
    "state": "invited_to_screening",
    "last_message_id": "msg_abc123def456",
    "updated_at": "2026-03-13T14:30:00.000Z"
  },
  "vacancy": {
    "id": "v_987654321",
    "name": "Backend Engineer (Go) — Senior",
    "alternate_url": "https://hh.ru/vacancies/123456789",
    "area": {"id": "a_moscow", "name": "Москва и МО"},
    "salary": {
      "from": 120000,
      "to": 250000,
      "currency": "RUB",
      "gross": false
    }
  },
  "employer": {
    "id": "e_456789123",
    "name": "АйТиХаб (IT-Hub)",
    "site_url": "https://it-hub.ru",
    "alternate_url": "https://hh.ru/company/456789123"
  },
  "contacts": {
    "from_message": {
      "signature_name": "Алексей Петров, Team Lead Backend",
      "emails": [
        "alexey.petrov@it-hub.ru",
        "a.petrov.direct@gmail.com"
      ],
      "telegram_urls": [
        "https://t.me/ITHubRecruit"
      ],
      "telegram_handles": [],
      "phones": [
        "+79161234567",
        "+79039876543"
      ],
      "urls": [],
      "contact_lines": [
        "Привет! Я Алексей — тимлид бэка в IT-Hub.",
        "Интересно к вакансии? Напиши мне напрямую, чтобы обсудить детали.",
        "Мой Telegram: https://t.me/ITHubRecruit",
        "Также можно на почту: alexey.petrov@it-hub.ru"
      ]
    },
    "from_vacancy": {
      "contacts": [],
      "raw_contacts": "{}"
    },
    "from_employer_site": []
  },
  "employer_tail": {
    "messages": [
      {
        "id": "msg_xyz987654",
        "created_at": "2026-03-13T14:28:45.123Z",
        "text": "Приветствую! Спасибо за отклик на позицию Backend Engineer (Go). Мы ищем опытных разработчиков с 5+ годами работы в продакшене."
      },
      {
        "id": "msg_abc123def456",
        "created_at": "2026-03-13T14:30:12.789Z",
        "text": "Кстати, я уже вижу твой профиль — очень релевантный стек. У тебя есть опыт с microservices и Kubernetes? Если интересно, пиши мне напрямую на Telegram, там быстрее обсудим технические детали интервью. Мой контакт: https://t.me/ITHubRecruit"
      }
    ],
    "text": "Приветствую! Спасибо за отклик... (весь текст)"
  }
}
EOF

curl -X POST \
  "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-HH-Applicant-Event: recruiter_contact_offer" \
  -H "X-Idempotency-Key: $IDEMPOTENCY_KEY" \
  -H "X-Webhook-Secret: $SECRET" \
  --data-binary @/tmp/webhook_payload.json
