# admin-ui (Next.js standalone): the platform admin & authoring console. The browser only talks to this service;
# /agent-factory/api/backend/* proxies to registry-service (INTERNAL_REGISTRY_URL) and the auth-service
# (INTERNAL_AUTH_URL) inside the cluster, so no backend URL and no token is ever baked into the browser bundle.

FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:20-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# Base path is baked into the bundle at build time (next.config.js default /agent-factory). Override per environment
# with --build-arg NEXT_BASE_PATH=/something; it must match the chart's adminUi route and NEXT_BASE_PATH config.
ARG NEXT_BASE_PATH=/agent-factory
ENV NEXT_BASE_PATH=$NEXT_BASE_PATH
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
