##########################
# Build the release image.
FROM python:3.12-slim-bullseye
LABEL MAINTAINER="AnJia <anjia0532@gmail.com>"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/opt/discovery-syncer/
ENV TZ=Asia/Shanghai
ENV PROJECT_ENV=prod

WORKDIR /opt/discovery-syncer/
COPY requirements.txt /opt/discovery-syncer/

# 刻意不替换 apt 源：换成国内镜像站在 GitHub Actions runner（境外 IP）上
# 会导致 apt-get update 失败（exit 100）。如需国内构建可用构建参数换源：
#   --build-arg APT_MIRROR=mirrors.ustc.edu.cn
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        if [ -f /etc/apt/sources.list ]; then \
            sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
        fi; \
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
        fi; \
    fi; \
    pip install -i https://mirrors.aliyun.com/pypi/simple --no-cache-dir --upgrade -r /opt/discovery-syncer/requirements.txt


ADD . /opt/discovery-syncer/

# ENTRYPOINT 用 exec 形式，不能带 shell 重定向：原写法把 ">/dev/stdout" 作为
# 第二个参数传给 python3，运行时会报「can't open file '>/dev/stdout'」。
# 容器日志默认就是 stdout，无需重定向。
ENTRYPOINT ["python3", "/opt/discovery-syncer/main.py"]


