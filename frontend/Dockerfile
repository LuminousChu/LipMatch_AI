# 第一階段：以 tsc 把 src/*.ts 編譯成 dist/*.js（只在建置時使用 Node，執行期不需要）
FROM node:20.18.1-alpine AS build
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# 開發用（只在 docker-compose.dev.yml 指定 target: dev 時建置；正式建置用最後一個階段）：
# 原始碼由 compose 掛載到 /srv/frontend，tsc --watch 存檔即重新編譯，index.html／styles.css 以連結指向掛載檔。
# Windows 掛載的資料夾收不到檔案事件，改用輪詢偵測變更。
FROM nginx:1.27.3-alpine AS dev
RUN apk add --no-cache nodejs npm
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY nginx.conf /etc/nginx/conf.d/default.conf
ENV TSC_WATCHFILE=DynamicPriorityPolling TSC_WATCHDIRECTORY=DynamicPriorityPolling
EXPOSE 80
CMD ["sh", "-c", "ln -sf /srv/frontend/index.html /srv/frontend/styles.css /usr/share/nginx/html/ && /build/node_modules/.bin/tsc -p /srv/frontend/tsconfig.json --outDir /usr/share/nginx/html --watch --preserveWatchOutput & exec nginx -g 'daemon off;'"]

# 第二階段：nginx 只提供靜態檔
FROM nginx:1.27.3-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html styles.css /usr/share/nginx/html/
# 編譯後的模組（app.js、api.js、pages/*.js…）整個目錄複製
COPY --from=build /build/dist/ /usr/share/nginx/html/
EXPOSE 80
