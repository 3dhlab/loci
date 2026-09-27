FROM node:22-alpine

WORKDIR /web

COPY package.json package-lock.json /web/
RUN npm ci

COPY . /web

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
