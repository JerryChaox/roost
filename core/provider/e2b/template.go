package e2b

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/JerryChaox/roost/core/provider"
	"github.com/JerryChaox/roost/core/provider/e2b/internal/api"
)

const (
	buildPoll    = 3 * time.Second
	buildTimeout = 30 * time.Minute
)

// FindTemplate looks the name up and reports whether any of the template's
// builds is ready: E2B resolves a name even when no build of it ever finished.
func (p *Provider) FindTemplate(ctx context.Context, name string) (bool, error) {
	r, err := p.api.GetTemplatesAliasesAliasWithResponse(ctx, name)
	if err != nil {
		return false, fmt.Errorf("e2b find template: %w", err)
	}
	if r.StatusCode() == http.StatusNotFound {
		return false, nil
	}
	if r.JSON200 == nil {
		return false, apiError("find template", r.StatusCode(), r.Body)
	}
	return p.hasReadyBuild(ctx, r.JSON200.TemplateID)
}

func (p *Provider) hasReadyBuild(ctx context.Context, templateID string) (bool, error) {
	params := &api.GetTemplatesTemplateIDParams{}
	for {
		r, err := p.api.GetTemplatesTemplateIDWithResponse(ctx, templateID, params)
		if err != nil {
			return false, fmt.Errorf("e2b template %s: %w", templateID, err)
		}
		if r.JSON200 == nil {
			return false, apiError("template "+templateID, r.StatusCode(), r.Body)
		}
		for _, b := range r.JSON200.Builds {
			if b.Status == "ready" {
				return true, nil
			}
		}
		next := r.HTTPResponse.Header.Get("X-Next-Token")
		if next == "" || len(r.JSON200.Builds) == 0 {
			return false, nil
		}
		params.NextToken = &next
	}
}

// BuildTemplate builds spec with E2B's v2 build API: it requests a build of
// the name, uploads the files of every Copy step as a gzipped tar keyed by its
// SHA-256 (skipped when E2B already holds that hash), starts the build with
// the steps and waits until it is ready.
func (p *Provider) BuildTemplate(ctx context.Context, spec provider.TemplateSpec) error {
	steps := make([]api.TemplateStep, 0, len(spec.Steps))
	type upload struct {
		hash string
		data []byte
	}
	var uploads []upload
	for _, s := range spec.Steps {
		user := s.User
		if user == "" {
			user = "root"
		}
		switch {
		case s.Copy != nil && s.Run == "":
			data, err := tarGz(spec.Dir, s.Copy.Src)
			if err != nil {
				return fmt.Errorf("e2b build: copy %s: %w", s.Copy.Src, err)
			}
			sum := sha256.Sum256(data)
			hash := hex.EncodeToString(sum[:])
			uploads = append(uploads, upload{hash, data})
			steps = append(steps, api.TemplateStep{
				Type:      "COPY",
				Args:      &[]string{s.Copy.Src, s.Copy.Dest, user, ""},
				FilesHash: &hash,
			})
		case s.Run != "" && s.Copy == nil:
			steps = append(steps, api.TemplateStep{Type: "RUN", Args: &[]string{s.Run, user}})
		default:
			return errors.New("e2b build: a step has exactly one of run and copy")
		}
	}

	cpu, mem := api.CPUCount(spec.CPU), api.MemoryMB(spec.MemoryMB)
	req, err := p.api.PostV3TemplatesWithResponse(ctx, api.TemplateBuildRequestV3{Name: &spec.Name, CpuCount: &cpu, MemoryMB: &mem})
	if err != nil {
		return fmt.Errorf("e2b build: request: %w", err)
	}
	if req.JSON202 == nil {
		return apiError("build request", req.StatusCode(), req.Body)
	}
	tid, bid := req.JSON202.TemplateID, req.JSON202.BuildID

	for _, u := range uploads {
		if err := p.uploadFiles(ctx, tid, u.hash, u.data); err != nil {
			return err
		}
	}

	start, err := p.api.PostV2TemplatesTemplateIDBuildsBuildIDWithResponse(ctx, tid, bid, api.TemplateBuildStartV2{
		FromImage: &spec.Image,
		Steps:     &steps,
	})
	if err != nil {
		return fmt.Errorf("e2b build: start: %w", err)
	}
	if start.StatusCode() != http.StatusAccepted {
		return apiError("build start", start.StatusCode(), start.Body)
	}
	return p.waitBuild(ctx, tid, bid)
}

func (p *Provider) uploadFiles(ctx context.Context, templateID, hash string, data []byte) error {
	r, err := p.api.GetTemplatesTemplateIDFilesHashWithResponse(ctx, templateID, hash)
	if err != nil {
		return fmt.Errorf("e2b build: upload link: %w", err)
	}
	if r.JSON201 == nil {
		return apiError("build upload link", r.StatusCode(), r.Body)
	}
	if r.JSON201.Present {
		return nil
	}
	if r.JSON201.Url == nil {
		return errors.New("e2b build: upload link without a url")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, *r.JSON201.Url, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("e2b build: upload: %w", err)
	}
	if r.JSON201.Headers != nil {
		for k, v := range *r.JSON201.Headers {
			req.Header.Set(k, v)
		}
	}
	resp, err := p.http.Do(req)
	if err != nil {
		return fmt.Errorf("e2b build: upload: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("e2b build: upload: status %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	return nil
}

func (p *Provider) waitBuild(ctx context.Context, tid, bid string) error {
	ctx, cancel := context.WithTimeout(ctx, buildTimeout)
	defer cancel()
	var offset int32
	var lastLog string
	for {
		params := &api.GetTemplatesTemplateIDBuildsBuildIDStatusParams{LogsOffset: &offset}
		st, err := p.api.GetTemplatesTemplateIDBuildsBuildIDStatusWithResponse(ctx, tid, bid, params)
		if err != nil {
			return fmt.Errorf("e2b build %s: status: %w", bid, err)
		}
		if st.JSON200 == nil {
			return apiError("build status", st.StatusCode(), st.Body)
		}
		offset += int32(len(st.JSON200.LogEntries))
		for _, e := range st.JSON200.LogEntries {
			if e.Level == "error" || e.Level == "warn" {
				lastLog = strings.TrimSpace(e.Message)
			}
		}
		switch st.JSON200.Status {
		case "ready":
			return nil
		case "error":
			reason := lastLog
			if st.JSON200.Reason != nil {
				reason = st.JSON200.Reason.Message
			}
			return fmt.Errorf("e2b build %s failed: %s", bid, reason)
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("e2b build %s: %w", bid, ctx.Err())
		case <-time.After(buildPoll):
		}
	}
}

// tarGz archives src (a file or a directory, relative to dir) as a gzipped
// tar with paths relative to dir. The archive is deterministic (sorted
// entries, fixed times and owners), so the same files give the same hash and
// E2B's upload cache hits.
func tarGz(dir, src string) ([]byte, error) {
	if !filepath.IsLocal(src) {
		return nil, fmt.Errorf("%q is not a path inside the template directory", src)
	}
	root := filepath.Join(dir, src)
	var paths []string
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		paths = append(paths, p)
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Strings(paths)
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf) // no name, no time in the header
	tw := tar.NewWriter(zw)
	epoch := time.Unix(946684800, 0) // 2000-01-01, fixed
	for _, p := range paths {
		info, err := os.Lstat(p)
		if err != nil {
			return nil, err
		}
		rel, err := filepath.Rel(dir, p)
		if err != nil {
			return nil, err
		}
		name := filepath.ToSlash(rel)
		hdr := &tar.Header{Name: name, ModTime: epoch, Format: tar.FormatPAX}
		switch {
		case info.IsDir():
			hdr.Typeflag, hdr.Name, hdr.Mode = tar.TypeDir, name+"/", 0o755
		case info.Mode().IsRegular():
			hdr.Typeflag, hdr.Size = tar.TypeReg, info.Size()
			hdr.Mode = 0o644
			if info.Mode()&0o111 != 0 {
				hdr.Mode = 0o755
			}
		case info.Mode()&fs.ModeSymlink != 0:
			target, err := os.Readlink(p)
			if err != nil {
				return nil, err
			}
			hdr.Typeflag, hdr.Linkname, hdr.Mode = tar.TypeSymlink, target, 0o777
		default:
			return nil, fmt.Errorf("%s: not a regular file, directory or symlink", path.Clean(name))
		}
		if err := tw.WriteHeader(hdr); err != nil {
			return nil, err
		}
		if hdr.Typeflag == tar.TypeReg {
			f, err := os.Open(p)
			if err != nil {
				return nil, err
			}
			_, err = io.Copy(tw, f)
			f.Close()
			if err != nil {
				return nil, err
			}
		}
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	if err := zw.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
