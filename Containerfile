FROM docker.io/library/golang:1.26 AS build

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags='-s -w' -o /out/collector ./cmd/collector

FROM docker.io/library/alpine:3.22 AS runtime

RUN apk add --no-cache ca-certificates \
    && addgroup -S -g 10001 collector \
    && adduser -S -D -H -u 10001 -G collector collector

COPY --from=build /out/collector /usr/local/bin/collector

VOLUME ["/spool"]
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/collector"]
