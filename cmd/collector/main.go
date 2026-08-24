package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/zzzubair/clashlens/internal/collector"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if err := collector.RunCLI(ctx, os.Args[1:], os.Getenv, os.Stdout, os.Stderr); err != nil {
		_, _ = fmt.Fprintf(os.Stderr, "collector: %v\n", err)
		os.Exit(1)
	}
}
