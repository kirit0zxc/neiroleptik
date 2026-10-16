# CI использует официальное зеркало, локальная сборка — Docker Hub.
ARG NODE_IMAGE=node:24-alpine
ARG NGINX_IMAGE=nginx:1.28-alpine
FROM ${NODE_IMAGE} AS build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY contracts/ /app/contracts/
# Демонстрационная сборка всегда использует настоящий центр.
ENV VITE_API_MOCK=0 \
    VITE_TELEMETRY_ENABLED=0 \
    VITE_EVENTS_ENABLED=1 \
    VITE_INVESTIGATION_ENABLED=1 \
    VITE_REPLAY_ENABLED=1 \
    VITE_ANALYTICS_ENABLED=0
RUN npm run build

FROM ${NGINX_IMAGE}
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html
EXPOSE 8080
