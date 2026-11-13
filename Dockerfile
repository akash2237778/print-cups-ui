# QuickPrint - standard library only, no pip install needed.
FROM python:3.11-slim
WORKDIR /app
COPY app.py ipp.py index.html setup.html ./
COPY static ./static
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "app.py"]
