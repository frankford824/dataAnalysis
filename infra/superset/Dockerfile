FROM apache/superset:4.1.2

USER root
RUN pip install --no-cache-dir psycopg2-binary==2.9.10
COPY infra/superset/entrypoint.sh /usr/local/bin/commerce-superset-entrypoint
RUN chmod 0555 /usr/local/bin/commerce-superset-entrypoint
USER superset

ENTRYPOINT ["/usr/local/bin/commerce-superset-entrypoint"]
