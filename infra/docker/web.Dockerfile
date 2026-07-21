FROM node:22-alpine AS build
WORKDIR /workspace
COPY apps/web/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY apps/web/ ./
ARG VITE_API_BASE=/api/v1
ARG VITE_SUPERSET_URL=http://localhost:8088
ENV VITE_API_BASE=$VITE_API_BASE
ENV VITE_SUPERSET_URL=$VITE_SUPERSET_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/dist /usr/share/nginx/html
EXPOSE 8080
