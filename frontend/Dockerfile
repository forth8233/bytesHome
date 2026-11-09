# 阶段一：构建前端静态产物
FROM node:20-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# 阶段二：Nginx 托管前端 + 反代 /api
FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf
COPY web.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8233