# Message DB (https://github.com/message-db/message-db) v1.3.0 on stock Postgres 16.
# The SQL is vendored in vendor/ (see vendor/SOURCE.txt, vendor/SHA256SUMS, vendor/LICENSE): nothing is
# downloaded at build time. The SHA256SUMS file is verified during the build.
FROM postgres:16.10-bookworm
COPY vendor/ /opt/message-db-vendor/
RUN cd /opt/message-db-vendor && sha256sum -c --quiet SHA256SUMS \
 && mkdir -p /opt/message-db && cp -r database /opt/message-db/database \
 && chmod +x /opt/message-db/database/*.sh
COPY install-message-db.sh /docker-entrypoint-initdb.d/10-install-message-db.sh
RUN chmod +x /docker-entrypoint-initdb.d/10-install-message-db.sh
