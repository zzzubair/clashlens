package collector

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"strconv"
	"time"
)

func RunCLI(
	ctx context.Context,
	arguments []string,
	getenv func(string) string,
	stdout io.Writer,
	stderr io.Writer,
) error {
	if len(arguments) == 0 {
		return errors.New("collector command is required: run, enqueue, or maintenance")
	}
	logger := slog.New(slog.NewJSONHandler(stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	switch arguments[0] {
	case "run":
		flags := flag.NewFlagSet("collector run", flag.ContinueOnError)
		flags.SetOutput(stderr)
		role := flags.String("role", "both", "process role: both, scheduler, or worker")
		once := flags.Bool("once", false, "run one scheduler pass and drain available work")
		if err := flags.Parse(arguments[1:]); err != nil {
			return err
		}
		if flags.NArg() != 0 {
			return errors.New("collector run accepts no positional arguments")
		}
		config, err := loadConfig(getenv)
		if err != nil {
			return err
		}
		app, err := newApplication(ctx, config, logger)
		if err != nil {
			return err
		}
		defer app.close()
		if *once {
			if *role != "both" && *role != "scheduler" && *role != "worker" {
				return fmt.Errorf("unknown role %q", *role)
			}
			if *role == "both" || *role == "scheduler" {
				if err := app.schedulerOnce(ctx, time.Now().UTC()); err != nil {
					return err
				}
			}
			if *role == "both" || *role == "worker" {
				releaseKeyOwnership, err := app.store.acquireAPIKeyOwnership(ctx, config.keys)
				if err != nil {
					return err
				}
				defer releaseKeyOwnership()
				if err := app.drain(ctx); err != nil {
					return err
				}
			}
			return json.NewEncoder(stdout).Encode(map[string]string{"status": "complete"})
		}
		return app.run(ctx, *role)

	case "enqueue":
		flags := flag.NewFlagSet("collector enqueue", flag.ContinueOnError)
		flags.SetOutput(stderr)
		workType := flags.String("type", "live_refresh", "initial_collection or live_refresh")
		normalizedTag := flags.String("tag", "", "normalized Clash of Clans player tag")
		bypassCooldown := flags.Bool("bypass-cooldown", false, "create work even during cooldown")
		if err := flags.Parse(arguments[1:]); err != nil {
			return err
		}
		if flags.NArg() != 0 || *normalizedTag == "" {
			return errors.New("collector enqueue requires --tag and accepts no positional arguments")
		}
		config, err := loadConfig(getenv)
		if err != nil {
			return err
		}
		app, err := newApplication(ctx, config, logger)
		if err != nil {
			return err
		}
		defer app.close()
		result, err := app.store.enqueueInteractive(
			ctx,
			*workType,
			*normalizedTag,
			time.Now().UTC(),
			config.interactiveCooldown,
			*bypassCooldown,
		)
		if err != nil {
			return err
		}
		return json.NewEncoder(stdout).Encode(map[string]any{
			"job_id":     result.jobID,
			"attempt_id": result.attemptID,
			"reused":     result.reused,
		})

	case "maintenance":
		return runMaintenance(ctx, arguments[1:], getenv, stdout, stderr)
	default:
		return fmt.Errorf("unknown collector command %q", arguments[0])
	}
}

func runMaintenance(
	ctx context.Context,
	arguments []string,
	getenv func(string) string,
	stdout io.Writer,
	stderr io.Writer,
) error {
	if len(arguments) == 0 {
		return errors.New("maintenance command is required: list-failed, list-leases, requeue, or reset-processing")
	}
	config, err := loadMaintenanceConfig(getenv)
	if err != nil {
		return err
	}
	store, err := openStore(ctx, config.databaseURL, config.schemaVersion)
	if err != nil {
		return err
	}
	defer store.close()

	switch arguments[0] {
	case "list-failed":
		flags := flag.NewFlagSet("collector maintenance list-failed", flag.ContinueOnError)
		flags.SetOutput(stderr)
		limit := flags.Int("limit", 100, "maximum failed jobs to list")
		if err := flags.Parse(arguments[1:]); err != nil {
			return err
		}
		failures, err := store.listFailedWork(ctx, *limit)
		if err != nil {
			return err
		}
		encoder := json.NewEncoder(stdout)
		for _, failure := range failures {
			record := map[string]any{
				"job_id":        failure.jobID,
				"work_type":     failure.workType,
				"capacity_pool": failure.capacityPool,
				"updated_at":    failure.updatedAt,
			}
			if failure.attemptID.Valid {
				record["attempt_id"] = failure.attemptID.Int64
			}
			if failure.failureCategory.Valid {
				record["failure_category"] = failure.failureCategory.String
			}
			if err := encoder.Encode(record); err != nil {
				return err
			}
		}
		return nil

	case "list-leases":
		flags := flag.NewFlagSet("collector maintenance list-leases", flag.ContinueOnError)
		flags.SetOutput(stderr)
		limit := flags.Int("limit", 100, "maximum expired leases to list")
		if err := flags.Parse(arguments[1:]); err != nil {
			return err
		}
		leases, err := store.listStuckLeases(ctx, time.Now().UTC(), *limit)
		if err != nil {
			return err
		}
		encoder := json.NewEncoder(stdout)
		for _, lease := range leases {
			if err := encoder.Encode(map[string]any{
				"job_id":      lease.jobID,
				"work_type":   lease.workType,
				"lease_owner": lease.owner,
				"expired_at":  lease.expiredAt,
			}); err != nil {
				return err
			}
		}
		return nil

	case "requeue":
		flags := flag.NewFlagSet("collector maintenance requeue", flag.ContinueOnError)
		flags.SetOutput(stderr)
		jobID := flags.Int64("job-id", 0, "failed collector job ID")
		if err := flags.Parse(arguments[1:]); err != nil {
			return err
		}
		if *jobID < 1 {
			return errors.New("maintenance requeue requires a positive --job-id")
		}
		if err := store.requeueFailedJob(ctx, *jobID, time.Now().UTC()); err != nil {
			return err
		}
		return json.NewEncoder(stdout).Encode(map[string]string{"status": "requeued", "job_id": strconv.FormatInt(*jobID, 10)})

	case "reset-processing":
		flags := flag.NewFlagSet("collector maintenance reset-processing", flag.ContinueOnError)
		flags.SetOutput(stderr)
		processingJobID := flags.Int64("processing-job-id", 0, "failed Python processing job ID")
		if err := flags.Parse(arguments[1:]); err != nil {
			return err
		}
		if *processingJobID < 1 {
			return errors.New("maintenance reset-processing requires a positive --processing-job-id")
		}
		if err := store.resetProcessingJob(ctx, *processingJobID, time.Now().UTC()); err != nil {
			return err
		}
		return json.NewEncoder(stdout).Encode(map[string]string{
			"status":            "reset",
			"processing_job_id": strconv.FormatInt(*processingJobID, 10),
		})
	default:
		return fmt.Errorf("unknown maintenance command %q", arguments[0])
	}
}
