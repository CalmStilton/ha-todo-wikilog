FROM python:3.13-alpine
# tzdata so DISPLAY_TZ/TZ (e.g. Europe/London) work for dates and log timestamps.
RUN apk add --no-cache tzdata
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wikilog.py .
# A named volume mounted at /state on first use inherits this ownership.
RUN mkdir /state && chown 65534:65534 /state
USER 65534:65534
VOLUME /state
ENTRYPOINT ["python", "/app/wikilog.py"]
CMD ["run"]
