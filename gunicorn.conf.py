# Gunicorn reads this file automatically when Render runs "gunicorn app:app".
#
# One worker process with several threads: if one request is slow (for
# example waiting on an outside service), other visitors are still served
# instead of queueing behind it.
workers = 1
threads = 4
timeout = 60
