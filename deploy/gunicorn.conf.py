"""Dell Wyse 3040: one small HTTP worker, two threads, no disk heartbeat."""
bind = '127.0.0.1:5000'
workers = 1
worker_class = 'gthread'
threads = 2
timeout = 30
graceful_timeout = 30
keepalive = 2
worker_tmp_dir = '/run/ingecon-web'
accesslog = None  # Nginx already records requests; avoid duplicate eMMC writes.
errorlog = '-'
loglevel = 'warning'
forwarded_allow_ips = '127.0.0.1'
secure_scheme_headers = {}  # This portal is HTTP; do not infer HTTPS from headers.
proxy_protocol = False
preload_app = False
daemon = False
