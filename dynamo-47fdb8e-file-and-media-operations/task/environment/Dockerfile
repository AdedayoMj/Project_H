FROM public.ecr.aws/docker/library/ubuntu:24.04@sha256:0d39fcc8335d6d74d5502f6df2d30119ff4790ebbb60b364818d5112d9e3e932

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        ghostscript \
        libglib2.0-0 \
        libgl1 \
        python3 \
        python3-pip \
    && pip install --no-cache-dir --break-system-packages \
        numpy==2.2.6 \
        opencv-python-headless==4.12.0.88 \
        pikepdf==9.10.2 \
        pillow==11.3.0 \
        pymupdf==1.26.3 \
        pytest==8.4.1 \
        pytest-json-ctrf==0.3.5 \
        qrcode==8.2 \
        reportlab==4.4.3 \
        scipy==1.16.1 \
        scikit-image==0.25.2 \
    && rm -rf /var/lib/apt/lists/*

COPY build_instance.py /tmp/build_instance.py
RUN python3 /tmp/build_instance.py --root /app \
    && rm /tmp/build_instance.py \
    && mkdir -p /app/output

WORKDIR /app
