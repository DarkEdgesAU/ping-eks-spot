ARG BASE_TAG=2609
FROM pingidentity/pingaccess:${BASE_TAG}
COPY --chown=9031:0 profiles/pingaccess/ /opt/in/
