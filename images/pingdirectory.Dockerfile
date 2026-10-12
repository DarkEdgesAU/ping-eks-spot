ARG BASE_TAG=2609
FROM pingidentity/pingdirectory:${BASE_TAG}
COPY --chown=9031:0 profiles/pingdirectory/ /opt/in/
