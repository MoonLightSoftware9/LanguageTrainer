ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG APP_VERSION=0.5.0
LABEL org.opencontainers.image.title="dict" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV DICT_ROOT_DIR=/app \
    DICT_DATABASE_URL=sqlite:////data/dict.sqlite3 \
    DICT_USER_SETTINGS_PATH=/data/user_settings.json \
    DICT_ARGOS_ROOT=/data/argos-translate \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --home-dir /home/dict --shell /usr/sbin/nologin dict \
    && mkdir -p /data \
    && chown -R dict:dict /data /app

COPY requirements.txt /tmp/requirements.txt
# argostranslate is installed with --no-deps so its stanza(->torch) requirement
# is skipped; a torch-free stub 'stanza' (copied below) satisfies its unguarded
# import. Sentence segmentation uses spaCy, so torch (~1 GB) is never needed.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && sed -e '/^argostranslate$/d' /tmp/requirements.txt > /tmp/requirements-runtime.txt \
    && python -m pip install --no-cache-dir -r /tmp/requirements-runtime.txt \
    && python -m pip install --no-cache-dir --no-deps argostranslate \
    && python -m nltk.downloader -d /usr/local/share/nltk_data wordnet \
    && rm -f /tmp/requirements.txt /tmp/requirements-runtime.txt

# Torch-free stub for argostranslate's unguarded `import stanza`.
COPY stubs/stanza /tmp/stanza-stub
RUN python -c "import sysconfig, shutil, os; d = os.path.join(sysconfig.get_paths()['purelib'], 'stanza'); shutil.rmtree(d, ignore_errors=True); shutil.copytree('/tmp/stanza-stub', d)" \
    && rm -rf /tmp/stanza-stub

COPY --chown=dict:dict dict.py get_sounds.py translate.py lookup_parse.py /app/
COPY --chown=dict:dict dictapp/ /app/dictapp/
COPY --chown=dict:dict example.jpg /app/example.jpg
COPY --chown=dict:dict docs/ /app/docs/
COPY --chown=dict:dict vendor/ /app/vendor/
COPY --chown=dict:dict web/ /app/web/
COPY --chown=dict:dict SoundEn.lsa /app/SoundEn.lsa
COPY --chown=dict:dict 1_star.txt /app/1_star.txt
COPY --chown=dict:dict 2_star.txt /app/2_star.txt
COPY --chown=dict:dict 3_star.txt /app/3_star.txt
COPY --chown=dict:dict zen.txt /app/zen.txt
COPY --chown=dict:dict thinking-fast-and-slow.txt /app/thinking-fast-and-slow.txt
# Word lookups are served from the prebuilt dictionaries.db; the source StarDict
# files are only needed at build time to generate it, so they are not shipped.
COPY --chown=dict:dict dictionaries.db /app/dictionaries.db

USER dict

EXPOSE 8000

ENTRYPOINT ["python", "/app/dict.py"]
CMD ["server", "--host", "0.0.0.0", "--port", "8000"]
