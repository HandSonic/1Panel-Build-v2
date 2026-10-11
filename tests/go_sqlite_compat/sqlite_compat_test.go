package sqlitecompat

import (
    "errors"
    "os"
    "testing"

    "github.com/glebarez/sqlite"
    "gorm.io/gorm"
    "gorm.io/gorm/logger"
)

type entryV1 struct {
    ID uint `gorm:"primaryKey"`
    Name string `gorm:"uniqueIndex"`
}
func (entryV1) TableName() string { return "compat_entries" }

type entryV2 struct {
    ID uint `gorm:"primaryKey"`
    Name string `gorm:"uniqueIndex"`
    Status string `gorm:"default:ready"`
}
func (entryV2) TableName() string { return "compat_entries" }

func TestSQLiteCompatibility(t *testing.T) {
    path, phase := os.Getenv("SQLITE_COMPAT_DB"), os.Getenv("SQLITE_COMPAT_PHASE")
    if path == "" { t.Fatal("SQLITE_COMPAT_DB is required") }
    db, err := gorm.Open(sqlite.Open(path), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
    if err != nil { t.Fatal(err) }
    sqlDB, err := db.DB()
    if err != nil { t.Fatal(err) }
    sqlDB.SetMaxOpenConns(1)
    defer sqlDB.Close()
    var mode string
    if err := db.Raw("PRAGMA journal_mode=WAL").Scan(&mode).Error; err != nil || mode != "wal" {
        t.Fatalf("WAL mode: %q, %v", mode, err)
    }
    if err := db.Exec("PRAGMA foreign_keys=ON").Error; err != nil { t.Fatal(err) }
    switch phase {
    case "baseline":
        if err := db.AutoMigrate(&entryV1{}); err != nil { t.Fatal(err) }
        if err := db.Create(&entryV1{ID: 1, Name: "before-upgrade"}).Error; err != nil { t.Fatal(err) }
        if err := db.Exec("CREATE TABLE compat_children (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES compat_entries(id))").Error; err != nil { t.Fatal(err) }
        if err := db.Exec("PRAGMA user_version=1").Error; err != nil { t.Fatal(err) }
    case "upgrade":
        if err := db.AutoMigrate(&entryV2{}); err != nil { t.Fatal(err) }
        var old entryV2
        if err := db.First(&old, 1).Error; err != nil || old.Name != "before-upgrade" || old.Status != "ready" {
            t.Fatalf("preserve row/add default column: %+v, %v", old, err)
        }
        rollback := errors.New("deliberate transaction rollback")
        if err := db.Transaction(func(tx *gorm.DB) error {
            if err := tx.Create(&entryV2{ID: 2, Name: "rolled-back"}).Error; err != nil { return err }
            return rollback
        }); !errors.Is(err, rollback) { t.Fatalf("transaction rollback: %v", err) }
        var count int64
        if err := db.Model(&entryV2{}).Where("id = ?", 2).Count(&count).Error; err != nil || count != 0 {
            t.Fatalf("rolled-back row persisted: %d, %v", count, err)
        }
        if err := db.Create(&entryV2{ID: 3, Name: "after-upgrade", Status: "verified"}).Error; err != nil { t.Fatal(err) }
        if err := db.Create(&entryV2{ID: 4, Name: "after-upgrade"}).Error; err == nil { t.Fatal("unique constraint not enforced") }
        if err := db.Exec("INSERT INTO compat_children VALUES (1, 999)").Error; err == nil { t.Fatal("foreign key not enforced") }
        if err := db.Exec("INSERT INTO compat_children VALUES (1, 1)").Error; err != nil { t.Fatal(err) }
        if err := db.Exec("PRAGMA user_version=2").Error; err != nil { t.Fatal(err) }
    case "rollback":
        // The original driver reopens and writes the upgraded database. This
        // tests engine rollback, not reversal of application schema migrations.
        var entries []entryV1
        if err := db.Order("id").Find(&entries).Error; err != nil || len(entries) != 2 {
            t.Fatalf("old driver reading upgraded database: %+v, %v", entries, err)
        }
        if entries[0].Name != "before-upgrade" || entries[1].Name != "after-upgrade" { t.Fatalf("data changed: %+v", entries) }
        if err := db.Create(&entryV1{ID: 5, Name: "old-driver-write"}).Error; err != nil { t.Fatal(err) }
        var version int
        if err := db.Raw("PRAGMA user_version").Scan(&version).Error; err != nil || version != 2 { t.Fatalf("user_version: %d, %v", version, err) }
    default:
        t.Fatalf("unknown phase %q", phase)
    }
    var integrity string
    if err := db.Raw("PRAGMA integrity_check").Scan(&integrity).Error; err != nil || integrity != "ok" {
        t.Fatalf("integrity: %q, %v", integrity, err)
    }
    if err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)").Error; err != nil { t.Fatal(err) }
    t.Logf("%s: WAL, integrity and database operations passed", phase)
}
