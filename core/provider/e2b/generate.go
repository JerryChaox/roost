package e2b

// The clients under internal/ are generated from E2B's own specs, copied into
// spec/ from the commit named in spec/SOURCE. Regenerate with go generate.

//go:generate go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.8.0 -config internal/api/cfg.yaml spec/openapi.yml
//go:generate go run github.com/bufbuild/buf/cmd/buf@v1.57.0 generate
