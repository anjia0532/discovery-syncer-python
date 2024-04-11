#!/usr/bin/env bash

version=${@: -1}

echo "Building docker image for discovery-syncer-python:${version}"

docker build  -f Dockerfile -t anjia0532/discovery-syncer-python:${version} ../../
docker push anjia0532/discovery-syncer-python:${version}

echo "https://hub.docker.com/r/anjia0532/discovery-syncer-python/tags?page=&page_size=&ordering=&name=${version}"