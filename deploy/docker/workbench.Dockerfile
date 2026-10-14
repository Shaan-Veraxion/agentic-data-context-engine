# Review workbench (Next.js standalone server). The API location is read at runtime from ADCE_API_INTERNAL.
# Build from the repository root:  docker build -f deploy/docker/workbench.Dockerfile -t adce-workbench .
FROM node:24-alpine AS build
WORKDIR /app
RUN npm install -g pnpm@9.15.9
COPY apps/workbench/package.json apps/workbench/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/workbench ./
RUN pnpm build

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production PORT=3001 HOSTNAME=0.0.0.0
RUN addgroup -S adce && adduser -S adce -G adce
COPY --from=build --chown=adce:adce /app/.next/standalone ./
COPY --from=build --chown=adce:adce /app/.next/static ./.next/static
USER adce
EXPOSE 3001
CMD ["node", "server.js"]
