//go:build linux

package collector

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// openSpoolRelative is the Linux trust boundary for descendant access. The
// root descriptor is reopened for each operation; Openat2 resolves every
// component beneath that inode and rejects symlink substitution races.
func openSpoolRelative(root, path string, flags int, mode uint32) (int, error) {
	if !filepath.IsAbs(path) {
		path = filepath.Join(root, path)
	}
	relative, err := filepath.Rel(root, path)
	if err != nil || relative == ".." || len(relative) >= 3 && relative[:3] == ".."+string(filepath.Separator) {
		return -1, errors.New("spool path escapes root")
	}
	rootFD, err := unix.Open(root, unix.O_PATH|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, err
	}
	defer unix.Close(rootFD)
	return unix.Openat2(rootFD, relative, &unix.OpenHow{
		Flags:   uint64(flags) | unix.O_CLOEXEC,
		Mode:    uint64(mode),
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS,
	})
}

func linkSpoolRelative(root, source, destination string) error {
	sourceDir, err := openSpoolRelative(root, filepath.Dir(source), unix.O_PATH|unix.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer unix.Close(sourceDir)
	destinationDir, err := openSpoolRelative(root, filepath.Dir(destination), unix.O_PATH|unix.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer unix.Close(destinationDir)
	return unix.Linkat(sourceDir, filepath.Base(source), destinationDir, filepath.Base(destination), 0)
}

func renameSpoolRelative(root, source, destination string) error {
	sourceDir, err := openSpoolRelative(root, filepath.Dir(source), unix.O_PATH|unix.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer unix.Close(sourceDir)
	destinationDir, err := openSpoolRelative(root, filepath.Dir(destination), unix.O_PATH|unix.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer unix.Close(destinationDir)
	return unix.Renameat(sourceDir, filepath.Base(source), destinationDir, filepath.Base(destination))
}

// readSpoolRelative reads a descendant file through a trusted root
// descriptor so no absolute traversal happens after the root is opened.
func readSpoolRelative(root, path string, limit int64) ([]byte, error) {
	fd, err := openSpoolRelative(root, path, unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	defer unix.Close(fd)
	if limit <= 0 {
		limit = 8 << 20
	}
	file := os.NewFile(uintptr(fd), "")
	defer file.Close()
	return io.ReadAll(io.LimitReader(file, limit))
}

// statSpoolRelative stats a descendant without following any symlink
// component; follow=false reports the link itself like os.Lstat.
func statSpoolRelative(root, path string, follow bool) (os.FileInfo, error) {
	flags := unix.O_PATH | unix.O_CLOEXEC
	if !follow {
		flags |= unix.O_NOFOLLOW
	}
	fd, err := openSpoolRelative(root, path, flags, 0)
	if err != nil {
		return nil, err
	}
	defer unix.Close(fd)
	file := os.NewFile(uintptr(fd), "")
	defer file.Close()
	return file.Stat()
}

// mkdirAllSpoolRelative creates each missing component of a descendant path
// with mkdirat beneath the trusted root descriptor.
func mkdirAllSpoolRelative(root, path string, mode uint32) error {
	if !filepath.IsAbs(path) {
		path = filepath.Join(root, path)
	}
	relative, err := filepath.Rel(root, path)
	if err != nil || relative == ".." || len(relative) >= 3 && relative[:3] == ".."+string(filepath.Separator) {
		return errors.New("spool path escapes root")
	}
	rootFD, err := unix.Open(root, unix.O_PATH|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer unix.Close(rootFD)
	dirFD := rootFD
	owned := false
	defer func() {
		if owned {
			unix.Close(dirFD)
		}
	}()
	for _, part := range strings.Split(relative, string(filepath.Separator)) {
		err := unix.Mkdirat(dirFD, part, mode)
		if err == nil || err == unix.EEXIST {
			var next int
			next, err = unix.Openat(dirFD, part, unix.O_PATH|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
			if err != nil {
				return err
			}
			if owned {
				unix.Close(dirFD)
			} else {
				owned = true
			}
			dirFD = next
			continue
		}
		// ENOENT/ENOTDIR mean a component was substituted between validation
		// and use; either way the caller must not proceed.
		return err
	}
	return nil
}

// listSpoolRelative lists a descendant directory through a trusted root
// descriptor. Entries never contain absolute paths.
func listSpoolRelative(root, dir string) ([]os.DirEntry, error) {
	fd, err := openSpoolRelative(root, dir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, os.ErrNotExist
		}
		return nil, err
	}
	// Name the file handle after the real directory so DirEntry.Info()
	// resolves against the validated root instead of an unrelated CWD.
	file := os.NewFile(uintptr(fd), filepath.Join(root, dir))
	defer file.Close()
	return file.ReadDir(-1)
}

// openReservationRecordRelative opens a control record read-write through the
// trusted root descriptor so lock-based crash reconciliation never traverses
// an absolute path that a concurrent substitution could redirect.
func openReservationRecordRelative(root, path string) (*os.File, error) {
	fd, err := openSpoolRelative(root, path, unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), path), nil
}

func unlinkSpoolRelative(root, path string) error {
	directory, err := openSpoolRelative(root, filepath.Dir(path), unix.O_PATH|unix.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	defer unix.Close(directory)
	return unix.Unlinkat(directory, filepath.Base(path), 0)
}
