# hermes-bridge Mattermost server plugin
#
# Builds the Go server component for linux/{amd64,arm64} and bundles a
# distributable tar.gz per the Mattermost plugin convention
# (dist/<plugin_id>-<version>.tar.gz).

PLUGIN_ID   ?= hermes-bridge
PLUGIN_VERSION ?= 0.1.0
BUNDLE_NAME ?= $(PLUGIN_ID)-$(PLUGIN_VERSION).tar.gz

export GO111MODULE=on

.PHONY: server bundle dist clean

server:
	rm -rf server/dist
	mkdir -p server/dist
	cd server && env CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o dist/plugin-linux-amd64 .
	cd server && env CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -o dist/plugin-linux-arm64 .

bundle:
	rm -rf dist
	mkdir -p dist/$(PLUGIN_ID)
	cp plugin.json dist/$(PLUGIN_ID)/
	cp -r server/dist dist/$(PLUGIN_ID)/server
	cd dist && tar -czf $(BUNDLE_NAME) $(PLUGIN_ID)

dist: server bundle

clean:
	rm -fr dist server/dist