# BASE_IMAGE must be the reviewed Ubuntu 24.04 image digest. prepare_context.py
# refuses mutable tags; the resulting local image digest must also be qualified.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install --no-install-recommends -y \
    ca-certificates git python3 perl make gcc g++ clang-18 binutils \
    libtinfo6 libncurses6 libx11-6 libxext6 libxrender1 libxtst6 libxi6 \
    libglib2.0-0t64 libfontconfig1 libfreetype6 libsm6 libice6 libnss3 \
    zlib1g libstdc++6 unzip time \
    && ln -s /usr/bin/clang-18 /usr/local/bin/clang \
    && ln -s /usr/bin/clang++-18 /usr/local/bin/clang++ \
    && mkdir -p /opt/coral-ci /job/source /job/metadata /job/build \
       /job/qualification /job/tmp /control /inbox /work/output \
    && dpkg-query -W -f='${binary:Package}\t${Version}\n' > /opt/coral-ci/os-packages.tsv \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=0:0 entrypoint.py trusted-identity.json /opt/coral-ci/
COPY --chown=0:0 trusted/ /opt/coral-ci/trusted/
COPY --chown=0:0 lib/ /opt/coral-ci/lib/
COPY --chown=0:0 bin/ /opt/coral-ci/bin/
RUN chmod -R a-w /opt/coral-ci && chmod 0555 /opt/coral-ci/bin/* \
    && /usr/bin/python3 -I /opt/coral-ci/lib/verify_image.py
ENV HOME=/job/tmp/home AWS_EC2_METADATA_DISABLED=true PYTHONDONTWRITEBYTECODE=1
USER 23001:23001
WORKDIR /job/source
ENTRYPOINT ["/opt/coral-ci/bin/build"]
