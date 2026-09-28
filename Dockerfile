# The frontend bundle is platform independent: build it once on the build
# host, even when the image targets another architecture.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build \
    && find dist/assets -type f \( -name '*.js' -o -name '*.css' \) \
        -exec gzip -9 -k '{}' \;

# JRE for the managed Suwayomi-Server JAR.
FROM eclipse-temurin:25-jre-jammy AS java-runtime

FROM python:3.13-slim-bookworm AS runtime
# PYTHONSAFEPATH: helper processes started with `python -m tankarr.…` import
# the installed package, never code from the working directory.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONSAFEPATH=1 \
    JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:$PATH \
    TANKARR_HOST=0.0.0.0 \
    TANKARR_PORT=8787 \
    TANKARR_DATA_DIR=/config \
    TANKARR_LIBRARY_DIR=/library \
    TANKARR_IMPORT_DIR=/import \
    TANKARR_FRONTEND_DIR=/app/frontend/dist

# RAR and RAR5 books are read with libarchive (bsdtar): maintained in Debian
# main with security updates, unlike unrar from non-free, which is no longer
# installed. unar stays as a fallback and for lsar's structured page index.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        fontconfig \
        fonts-dejavu-core \
        libarchive-tools \
        libfreetype6 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        tesseract-ocr-all \
        unar \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 tankarr \
    && useradd --uid 1000 --gid 1000 --create-home tankarr

COPY --from=java-runtime /opt/java/openjdk /opt/java/openjdk

# Dependencies change far less often than the application: install them in
# their own layer so an update only downloads the small application layers.
WORKDIR /app
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY README.md ./
COPY tankarr/ ./tankarr/
RUN pip install --no-cache-dir --no-deps . \
    && mkdir -p /config /library /import /downloads \
    && chown tankarr:tankarr /config /library /import /downloads
COPY --from=frontend /src/frontend/dist ./frontend/dist/

# The commit the image was built from, shown on the System page.
ARG TANKARR_COMMIT=""
ENV TANKARR_BUILD_COMMIT=$TANKARR_COMMIT
LABEL org.opencontainers.image.title="Tankarr" \
      org.opencontainers.image.description="Self-hosted manga, manhwa and comics manager for the *arr stack" \
      org.opencontainers.image.url="https://github.com/Renzodef/tankarr" \
      org.opencontainers.image.source="https://github.com/Renzodef/tankarr" \
      org.opencontainers.image.documentation="https://renzodef.github.io/tankarr/" \
      org.opencontainers.image.licenses="GPL-3.0-only"

USER tankarr
EXPOSE 8787
VOLUME ["/config", "/library"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/api/ready || exit 1
CMD ["tankarr"]
