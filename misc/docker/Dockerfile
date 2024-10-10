##########################
# Build the release image.
FROM python:3.11-slim-bullseye
LABEL MAINTAINER AnJia <anjia0532@gmail.com>

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/opt/discovery-syncer/
ENV TZ=Asia/Shanghai
ENV PROJECT_ENV=prod

WORKDIR /opt/discovery-syncer/
COPY requirements.txt /opt/discovery-syncer/

RUN sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list && \
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-cache-dir --upgrade -r /opt/discovery-syncer/requirements.txt


ADD . /opt/discovery-syncer/

ENTRYPOINT ["python3", "/opt/discovery-syncer/main.py", ">/dev/stdout"]


