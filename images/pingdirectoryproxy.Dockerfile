ARG BASE_TAG=2609
FROM pingidentity/pingdirectoryproxy:${BASE_TAG}
COPY --chown=9031:0 profiles/pingdirectoryproxy/ /opt/in/
