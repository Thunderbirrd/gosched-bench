# Образ с тремя бинарниками стенда: server, loadgen, calib.
# Сборка в официальном образе Go фиксированной версии, запуск из scratch:
# статические бинарники, в образе нет ничего лишнего, что могло бы шуметь.
FROM golang:1.27.3-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -o /out/ ./cmd/...

FROM scratch
COPY --from=build /out/ /bin/
# server слушает 7070; результаты пишутся в /results (монтируется с хоста)
EXPOSE 7070
