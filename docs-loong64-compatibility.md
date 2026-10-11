# Automatic linux/loong64 SQLite compatibility

The build does not keep a 1Panel-version-to-dependency table. For each core and
agent module, `go_arch_compat.py` asks Go which files are selected for
`modernc.org/sqlite/lib` and `modernc.org/libc/errno` on linux/loong64.

- Other architectures and already supported source graphs use their original
  module files. A source that no longer uses modernc SQLite is left unchanged.
- Only a graph missing architecture-specific Go files gets a disposable module
  overlay. Source `go.mod` and `go.sum` are not edited.
- The compatibility floor is `modernc.org/sqlite v1.29.10` together with
  `modernc.org/libc v1.49.3`, the libc requirement of that SQLite release.
  Requirements are raised with `go mod edit`, followed by Go's minimal version
  selection. Original selected transitive dependency floors are retained in the
  temporary overlay if a newer modernc graph drops a requirement. Newer
  dependencies are retained; any remaining downgrade is rejected.
- Source replacements, unexpected probe errors, an unsupported newer graph, or
  an overlay still missing architecture support fail explicitly. There is no
  `@latest`, silent architecture omission, or substitution of another CPU target.
- Selected overlay versions appear in build logs and in Go's embedded module
  build information (`go version -m 1panel-core` / `1panel-agent`).

Upstream references:

- https://pkg.go.dev/modernc.org/sqlite (linux/loong64 support added in v1.29.7;
  libc must be compatible with the selected SQLite version)
- https://proxy.golang.org/modernc.org/sqlite/@v/v1.29.10.mod (paired libc pin)

## Verification

Pure Python regression tests run with the existing test suite:

    python3 -m unittest discover -s tests -v

With a prepared 1Panel source, the selected Go toolchain, and Ubuntu's official
`qemu-user` package installed:

    python3 scripts/verify_go_sqlite_compat.py prepared-source --qemu qemu-loongarch64

For both core and agent this runner:

1. Builds a linux/loong64 test binary using the architecture-selected module graph.
2. Creates a database with the original source graph on the host architecture.
3. Reopens it with the overlaid graph, migrates a GORM model, checks preserved
   data/default values, WAL, unique and foreign-key constraints, transaction
   rollback, `user_version`, and SQLite integrity.
4. Reopens and writes the resulting database with the original source graph.
5. Repeats the database roundtrip with step 3 executing as real linux/loong64
   machine code under QEMU, rather than substituting a host binary.
6. Confirms original source module/checksum files are unchanged.

The engine rollback check establishes that the original SQLite driver can read
and write this upgraded fixture. It does not reverse 1Panel's application schema
migrations or prove every application migration is reversible. This smoke test
supplements the normal full core/agent cross-compilation and package validation.
Without `--qemu`, native checks and target compilation run, and target execution
is explicitly reported as not run. CI should pass `--qemu` when claiming target
runtime validation.
