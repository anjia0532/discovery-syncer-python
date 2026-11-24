##########################
# Build the release image.
FROM python:3.12-slim-bookworm
LABEL MAINTAINER="AnJia <anjia0532@gmail.com>"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/opt/discovery-syncer/
ENV TZ=Asia/Shanghai
ENV PROJECT_ENV=prod

WORKDIR /opt/discovery-syncer/
COPY requirements.txt /opt/discovery-syncer/

# 刻意不替换 apt / pypi 源：国内镜像站在 GitHub Actions runner（境外 IP）上反而更慢，
# 且把 deb.debian.org 换成国内镜像会让 apt-get update 失败（exit 100）。
# 所以默认全走官方源（含官方 PyPI）；国内本地构建需要加速时显式覆盖：
#   --build-arg APT_MIRROR=mirrors.ustc.edu.cn
#   --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG APT_MIRROR=""
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN if [ -n "$APT_MIRROR" ]; then \
        if [ -f /etc/apt/sources.list ]; then \
            sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
        fi; \
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
        fi; \
    fi; \
    pip install -i "$PIP_INDEX_URL" --no-cache-dir --upgrade -r /opt/discovery-syncer/requirements.txt


ADD . /opt/discovery-syncer/

# ENTRYPOINT 用 exec 形式，不能带 shell 重定向：原写法把 ">/dev/stdout" 作为
# 第二个参数传给 python3，运行时会报「can't open file '>/dev/stdout'」。
# 容器日志默认就是 stdout，无需重定向。
ENTRYPOINT ["python3", "/opt/discovery-syncer/main.py"]


