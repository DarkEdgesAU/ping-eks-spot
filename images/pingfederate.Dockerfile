ARG BASE_TAG=2609
FROM pingidentity/pingfederate:${BASE_TAG}
COPY --chown=9031:0 profiles/pingfederate/ /opt/in/
