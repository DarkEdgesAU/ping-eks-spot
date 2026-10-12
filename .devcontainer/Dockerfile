FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip git make jq openssl python3-venv python3-pip \
    openjdk-21-jre-headless ldap-utils && rm -rf /var/lib/apt/lists/*
ARG TERRAFORM_VERSION=1.13.5
ARG HELM_VERSION=3.19.0
ARG KUBECTL_VERSION=1.35.0
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o /tmp/terraform.zip \
    && unzip /tmp/terraform.zip terraform -d /usr/local/bin && rm /tmp/terraform.zip \
    && curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" -o /tmp/helm.tgz \
    && tar -xzf /tmp/helm.tgz -C /tmp && mv /tmp/linux-amd64/helm /usr/local/bin/helm && rm -rf /tmp/helm.tgz /tmp/linux-amd64 \
    && curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/aws.zip \
    && unzip -q /tmp/aws.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/aws.zip
RUN python3 -m venv /opt/project-venv && /opt/project-venv/bin/pip install PyYAML==6.0.3
ENV PATH="/opt/project-venv/bin:${PATH}"
USER vscode
