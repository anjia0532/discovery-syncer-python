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

# 换 apt 源。Debian bookworm 起改用 /etc/apt/sources.list.d/debian.sources，
# 老路径不存在时 `sed -i` 会返回 2 并中断构建，因此对两种路径都做兼容。
RUN if [ -f /etc/apt/sources.list ]; then \
        sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list; \
    fi; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    pip install -i https://mirrors.aliyun.com/pypi/simple --no-cache-dir --upgrade -r /opt/discovery-syncer/requirements.txt


ADD . /opt/discovery-syncer/

# ENTRYPOINT 用 exec 形式，不能带 shell 重定向：原写法把 ">/dev/stdout" 作为
# 第二个参数传给 python3，运行时会报「can't open file '>/dev/stdout'」。
# 容器日志默认就是 stdout，无需重定向。
ENTRYPOINT ["python3", "/opt/discovery-syncer/main.py"]


