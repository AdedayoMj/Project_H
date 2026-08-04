FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libfreetype6 \
        libglib2.0-0 \
        libgl1 \
    && pip install --no-cache-dir \
        fonttools==4.63.0 \
        freetype-py==2.5.1 \
        numpy==2.2.6 \
        opencv-python-headless==4.12.0.88 \
        pikepdf==9.10.2 \
        pillow==11.3.0 \
        pymupdf==1.26.3 \
        pytest==8.4.1 \
        pytest-json-ctrf==0.3.5 \
        reportlab==4.4.3 \
        scipy==1.16.1 \
        ufoLib2==0.18.1 \
        uharfbuzz==0.55.0 \
    && rm -rf /var/lib/apt/lists/*

COPY build_instance.py font_model.py /tmp/dynamo-font-build/
RUN PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/dynamo-font-build \
        python3 /tmp/dynamo-font-build/build_instance.py --root /app \
    && rm -rf /tmp/dynamo-font-build \
    && ! find / -type f \
        \( -name 'font_model.py' -o -name 'font_model*.pyc' \
        -o -name 'build_instance.py' -o -name 'build_instance*.pyc' \) \
        -print -quit | grep -q . \
    && mkdir -p /app/output

WORKDIR /app
