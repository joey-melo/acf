FROM alpine:3.22
RUN apk add --no-cache python3 iptables
COPY guard.py /guard.py
HEALTHCHECK --interval=2s --timeout=2s --start-period=2s --retries=15 CMD test -f /run/ready
ENTRYPOINT ["python3", "/guard.py"]
