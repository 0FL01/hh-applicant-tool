FROM python:3.13-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
  gcc \
  libc6-dev \
  procps \
  cron \
  dos2unix \
  tzdata \
  less

# Настройка пользователя
ARG UID=1000
ARG GID=1000
RUN groupadd -g $GID docker && \
  useradd -u $UID -g docker -m -s /bin/bash docker

WORKDIR /app

# Копируем файлы пакета
COPY src /app/src
COPY hh_llm_agent /app/hh_llm_agent
COPY pyproject.toml poetry.lock* README.md /app/

# Устанавливаем пакет с зависимостями (включает hh_applicant_tool + hh_llm_agent)
RUN pip install --no-cache-dir '.[playwright,pillow]'

# Ставим зависимости хромиума и сам браузер в общий кэш
RUN mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" && \
  playwright install-deps chromium && \
  playwright install chromium

# Очистка кеша пакетов для уменьшения веса контейнера
RUN rm -rf /var/lib/apt/lists/*

# Fix: падение, если каталог config не существует
#RUN mkdir -p /app/config

# Копируем остальное (эти файлы мешают кешированию последующих слоев)
COPY crontab /app/crontab
COPY startup.sh /app/startup.sh

# Настройка крона
RUN touch /var/log/cron.log && chown docker:docker /var/log/cron.log && \
  dos2unix /app/crontab && \
  chmod +x /app/startup.sh && \
  chmod 0644 /app/crontab && \
  crontab -u docker /app/crontab

# Запускаем крон и читаем лог
# cron не видит переменные окружения, переданные главному процессу, точнее
# он начинает новую сессию, где тот же $CONFIG_DIR пуст
CMD printenv | grep -E 'CONFIG_DIR|HH_PROFILE_ID' >> /etc/environment && \
  mkdir -p /app/config && \
  chown -R docker:docker /app/config && \
  cron && \
  tail -f /var/log/cron.log
