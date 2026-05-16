PRAGMA foreign_keys = OFF;
-- На всякий случай выключаем проверки
BEGIN;
/* ===================== employers ===================== */
CREATE TABLE IF NOT EXISTS employers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    description TEXT,
    site_url TEXT,
    area_id INTEGER,
    area_name TEXT,
    alternate_url TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
/* ===================== contacts ===================== */
CREATE TABLE IF NOT EXISTS vacancy_contacts (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))) NOT NULL,
    vacancy_id INTEGER NOT NULL,
    -- Все это избыточные поля
    vacancy_alternate_url TEXT,
    vacancy_name TEXT,
    vacancy_area_id INTEGER,
    vacancy_area_name TEXT,
    vacancy_salary_from INTEGER,
    vacancy_salary_to INTEGER,
    vacancy_currency VARCHAR(3),
    vacancy_gross BOOLEAN,
    --
    employer_id INTEGER,
    employer_name TEXT,
    --
    name TEXT,
    email TEXT,
    phone_numbers TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (vacancy_id, email)
);
/* ===================== vacancies ===================== */
CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    area_id INTEGER,
    area_name TEXT,
    salary_from INTEGER,
    salary_to INTEGER,
    currency VARCHAR(3),
    gross BOOLEAN,
    published_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    remote BOOLEAN,
    experience TEXT,
    professional_roles TEXT,
    alternate_url TEXT
);
/* ===================== vacancy_response_dedup ===================== */
CREATE TABLE IF NOT EXISTS vacancy_response_dedup (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))) NOT NULL,
    resume_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    employer_id INTEGER,
    vacancy_id INTEGER NOT NULL,
    vacancy_name TEXT NOT NULL,
    alternate_url TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (resume_id, dedupe_key)
);
/* ===================== skipped_vacancies ===================== */
CREATE TABLE IF NOT EXISTS skipped_vacancies (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))) NOT NULL,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    alternate_url TEXT,
    name TEXT,
    employer_name TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (resume_id, vacancy_id)
);
/* ===================== negotiations ===================== */
CREATE TABLE IF NOT EXISTS negotiations (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    vacancy_id INTEGER NOT NULL,
    employer_id INTEGER,
    -- Может обнулиться при блокировке раб-о-тодателя
    chat_id INTEGER NOT NULL,
    resume_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
/* ===================== chat_messages ===================== */
CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    negotiation_id INTEGER NOT NULL,
    chat_id INTEGER,
    participant_type TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at DATETIME,
    viewed_by_opponent BOOLEAN
);
/* ===================== agent_runs ===================== */
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    model TEXT,
    dry_run BOOLEAN NOT NULL DEFAULT 0,
    total_negotiations INTEGER NOT NULL DEFAULT 0,
    replied_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME
);
/* ===================== agent_decisions ===================== */
CREATE TABLE IF NOT EXISTS agent_decisions (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    negotiation_id INTEGER NOT NULL,
    chat_id INTEGER,
    vacancy_id INTEGER,
    employer_id INTEGER,
    resume_id TEXT,
    last_message_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    reply_text TEXT,
    model TEXT,
    raw_response TEXT,
    reasoning_details TEXT NOT NULL DEFAULT '[]',
    classifier_category TEXT,
    classifier_reason TEXT,
    classifier_confidence REAL,
    classifier_model TEXT,
    classifier_raw_response TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (negotiation_id, last_message_id)
);
/* ===================== agent_outbox ===================== */
CREATE TABLE IF NOT EXISTS agent_outbox (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    negotiation_id INTEGER NOT NULL,
    chat_id INTEGER,
    source_last_message_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    send_after DATETIME,
    sent_at DATETIME,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (negotiation_id, source_last_message_id, sequence_no)
);
/* ===================== agent_webhooks ===================== */
CREATE TABLE IF NOT EXISTS agent_webhooks (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    negotiation_id INTEGER NOT NULL,
    chat_id INTEGER,
    source_last_message_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_url TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    send_after DATETIME,
    sent_at DATETIME,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (negotiation_id, source_last_message_id, event_type)
);
/* ===================== settings ===================== */
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
/* ===================== resumes ===================== */
CREATE TABLE IF NOT EXISTS resumes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT,
    alternate_url TEXT,
    status_id TEXT,
    status_name TEXT,
    can_publish_or_update BOOLEAN,
    total_views INTEGER DEFAULT 0,
    new_views INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
/* ===================== ИНДЕКСЫ ДЛЯ СТАТИСТИКИ ===================== */
-- Чтобы выборка для отправки на сервер по updated_at не тормозила
CREATE INDEX IF NOT EXISTS idx_vac_upd ON vacancies(updated_at);
CREATE INDEX IF NOT EXISTS idx_vacancy_response_dedup_resume_key ON vacancy_response_dedup(resume_id, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_skipped_vacancies_resume ON skipped_vacancies(resume_id, vacancy_id);
CREATE INDEX IF NOT EXISTS idx_emp_upd ON employers(updated_at);
CREATE INDEX IF NOT EXISTS idx_neg_upd ON negotiations(updated_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_neg ON chat_messages(negotiation_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_neg_msg ON agent_decisions(negotiation_id, last_message_id);
CREATE INDEX IF NOT EXISTS idx_agent_outbox_status_send_after ON agent_outbox(status, send_after);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at);
/* ===================== ТРИГГЕРЫ (Всегда обновляют дату) ===================== */
-- Убрал условие WHEN. Теперь при любом UPDATE дата актуализируется принудительно.
CREATE TRIGGER IF NOT EXISTS trg_resumes_updated
AFTER
UPDATE ON resumes BEGIN
UPDATE resumes
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_employers_updated
AFTER
UPDATE ON employers BEGIN
UPDATE employers
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_vacancy_contacts_updated
AFTER
UPDATE ON vacancy_contacts BEGIN
UPDATE vacancy_contacts
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_vacancies_updated
AFTER
UPDATE ON vacancies BEGIN
UPDATE vacancies
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_vacancy_response_dedup_updated
AFTER
UPDATE ON vacancy_response_dedup BEGIN
UPDATE vacancy_response_dedup
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_skipped_vacancies_updated
AFTER
UPDATE ON skipped_vacancies BEGIN
UPDATE skipped_vacancies
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_negotiations_updated
AFTER
UPDATE ON negotiations BEGIN
UPDATE negotiations
SET updated_at = CURRENT_TIMESTAMP
WHERE id = OLD.id;
END;
/* ===================== employer_sites ===================== */
CREATE TABLE IF NOT EXISTS employer_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employer_id INTEGER NOT NULL,
    site_url TEXT NOT NULL,
    ip_address TEXT,
    title TEXT,
    description TEXT,
    generator TEXT,
    server_name TEXT,
    powered_by TEXT,
    emails TEXT,
    subdomains TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    -- Уникальность пары: один работодатель — один конкретный сайт
    UNIQUE (employer_id, site_url)
);

/* ===================== ИНДЕКСЫ ===================== */
CREATE INDEX IF NOT EXISTS idx_emp_site_upd ON employer_sites(updated_at);

/* ===================== ТРИГГЕРЫ ===================== */
CREATE TRIGGER IF NOT EXISTS trg_employer_sites_updated
AFTER UPDATE ON employer_sites
BEGIN
    UPDATE employer_sites
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = OLD.id;
END;
COMMIT;
