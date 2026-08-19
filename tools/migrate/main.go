// Command migrate 是 W5-R 的迁移执行器（T-W5-032）：golang-migrate library 模式。
//
// 用法：
//
//	migrate -dsn "postgres://user:pass@host:port/db?sslmode=disable" -dir db/migrations up [N]
//	migrate ... down [N]   # down 必须显式给 N（防误降全库；全量演练用 make migrate-go-check）
//	migrate ... drop       # 危险：全量回滚（仅迁移演练使用）
//
// 与 alembic 双轨期语义基准：db/migrations/*.sql 由 alembic 在线捕获生成
// （tools/sql/gen_migrations_from_alembic.py），pg_dump schema diff 为空是
// 两者等价的可执行验收（make migrate-go-parity）。
package main

import (
	"errors"
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/golang-migrate/migrate/v4"
	_ "github.com/golang-migrate/migrate/v4/database/pgx/v5"
	_ "github.com/golang-migrate/migrate/v4/source/file"
)

func main() {
	dsn := flag.String("dsn", os.Getenv("MIGRATE_DSN"), "目标库 DSN（默认取 MIGRATE_DSN 环境变量）")
	dir := flag.String("dir", "db/migrations", "迁移目录")
	flag.Parse()
	args := flag.Args()
	if *dsn == "" || len(args) == 0 {
		fmt.Fprintln(os.Stderr, "用法: migrate -dsn <postgres-dsn> -dir db/migrations <up [N]|down N|drop>")
		os.Exit(2)
	}

	m, err := migrate.New("file://"+*dir, *dsn)
	if err != nil {
		log.Fatalf("初始化迁移器失败: %v", err)
	}
	defer m.Close()

	step := func(n int) int { return n }

	switch args[0] {
	case "up":
		if len(args) > 1 {
			var n int
			if _, err := fmt.Sscanf(args[1], "%d", &n); err != nil {
				log.Fatalf("up 参数须为整数: %v", err)
			}
			if err := m.Steps(step(n)); err != nil && !errors.Is(err, migrate.ErrNoChange) {
				log.Fatalf("up %d 步失败: %v", n, err)
			}
		} else if err := m.Up(); err != nil && !errors.Is(err, migrate.ErrNoChange) {
			log.Fatalf("up 全量失败: %v", err)
		}
	case "down":
		if len(args) < 2 {
			log.Fatal("down 必须显式给步数 N（防误降全库；全量演练走 make migrate-go-check）")
		}
		var n int
		if _, err := fmt.Sscanf(args[1], "%d", &n); err != nil {
			log.Fatalf("down 参数须为整数: %v", err)
		}
		if err := m.Steps(-n); err != nil && !errors.Is(err, migrate.ErrNoChange) {
			log.Fatalf("down %d 步失败: %v", n, err)
		}
	case "drop":
		if err := m.Drop(); err != nil && !errors.Is(err, migrate.ErrNoChange) {
			log.Fatalf("drop 失败: %v", err)
		}
	default:
		log.Fatalf("未知子命令 %q（up/down/drop）", args[0])
	}

	v, dirty, err := m.Version()
	if err != nil {
		if errors.Is(err, migrate.ErrNilVersion) {
			fmt.Println("version: 0（全部回滚）")
			return
		}
		log.Fatalf("读取版本失败: %v", err)
	}
	fmt.Printf("version: %d dirty: %v\n", v, dirty)
}
