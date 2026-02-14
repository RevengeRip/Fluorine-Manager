#include "fuseconnector.h"

#include "settings.h"
#include "vfs/vfstree.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QStandardPaths>
#include <QTextStream>
#include <QVariant>

#include <iplugingame.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <set>
#include <sys/stat.h>
#include <unistd.h>

using namespace MOBase;

// Global mount point for signal-handler cleanup (async-signal-safe access).
static char g_fuseMountPoint[4096] = {0};

void setFuseMountPointForCrashCleanup(const char* path)
{
  if (path != nullptr) {
    std::strncpy(g_fuseMountPoint, path, sizeof(g_fuseMountPoint) - 1);
    g_fuseMountPoint[sizeof(g_fuseMountPoint) - 1] = '\0';
  } else {
    g_fuseMountPoint[0] = '\0';
  }
}

const char* getFuseMountPointForCrashCleanup()
{
  return g_fuseMountPoint[0] != '\0' ? g_fuseMountPoint : nullptr;
}

namespace
{
namespace fs = std::filesystem;

bool isFlatpak()
{
  static const bool result = QFile::exists(QStringLiteral("/.flatpak-info"));
  return result;
}

bool waitForHelperLine(QProcess* proc, const char* expected, int timeoutMs)
{
  const QByteArray target(expected);
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::milliseconds(timeoutMs);

  while (proc->state() == QProcess::Running) {
    if (proc->canReadLine()) {
      const QByteArray line = proc->readLine().trimmed();
      if (line == target) {
        return true;
      }
      if (line.startsWith("error:")) {
        log::error("VFS helper: {}", QString::fromUtf8(line));
        return false;
      }
      continue;
    }

    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                               deadline - std::chrono::steady_clock::now())
                               .count();
    if (remaining <= 0) {
      break;
    }
    proc->waitForReadyRead(static_cast<int>(remaining));
  }

  return false;
}

bool sendHelperCommand(QProcess* proc, const char* command, int timeoutMs)
{
  proc->write(command);
  proc->write("\n");
  if (!proc->waitForBytesWritten(1000)) {
    return false;
  }
  return waitForHelperLine(proc, "ok", timeoutMs);
}

std::string decodeProcMountField(const std::string& in)
{
  std::string out;
  out.reserve(in.size());

  for (size_t i = 0; i < in.size();) {
    if (in[i] == '\\' && i + 3 < in.size() && std::isdigit(in[i + 1]) &&
        std::isdigit(in[i + 2]) && std::isdigit(in[i + 3])) {
      const std::string oct = in.substr(i + 1, 3);
      const int value       = std::stoi(oct, nullptr, 8);
      out.push_back(static_cast<char>(value));
      i += 4;
      continue;
    }

    out.push_back(in[i]);
    ++i;
  }

  return out;
}

bool isMountPoint(const QString& path)
{
  QFile mounts(QStringLiteral("/proc/mounts"));
  if (!mounts.open(QIODevice::ReadOnly)) {
    return false;
  }

  const auto mountPoint = QDir::cleanPath(path);
  while (!mounts.atEnd()) {
    const auto line  = QString::fromUtf8(mounts.readLine()).trimmed();
    const auto parts = line.split(' ', Qt::SkipEmptyParts);
    if (parts.size() < 2) {
      continue;
    }

    const QString current = QString::fromStdString(
        decodeProcMountField(parts[1].toStdString()));
    if (QDir::cleanPath(current) == mountPoint) {
      return true;
    }
  }

  return false;
}

bool runUnmountCommand(const QString& program, const QStringList& args)
{
  // Suppress stderr from fusermount/umount to avoid confusing terminal output
  // when unmount fails (e.g. permission denied in Flatpak sandbox).
  auto tryRun = [&](const QString& cmd, const QStringList& cmdArgs) -> bool {
    QProcess p;
    p.setStandardErrorFile(QProcess::nullDevice());
    p.start(cmd, cmdArgs);
    if (!p.waitForFinished(3000)) {
      p.kill();
      return false;
    }
    return p.exitStatus() == QProcess::NormalExit && p.exitCode() == 0;
  };

  // In Flatpak: try sandbox-local unmount first (mount was likely created
  // inside the sandbox), then fall back to host-side unmount.
  if (isFlatpak()) {
    if (tryRun(program, args)) {
      return true;
    }
    QStringList spawnArgs = {QStringLiteral("--host"), program};
    spawnArgs.append(args);
    return tryRun(QStringLiteral("flatpak-spawn"), spawnArgs);
  }

  return tryRun(program, args);
}

std::vector<std::pair<std::string, std::string>>
buildModsFromMapping(const MappingType& mapping, const QString& dataDir,
                     const QString& overwriteDir)
{
  std::vector<std::pair<std::string, std::string>> mods;
  std::set<std::string> seen;

  const QString dataPrefix = QDir::cleanPath(dataDir) + "/";
  const QString overPrefix = QDir::cleanPath(overwriteDir) + "/";

  for (const auto& map : mapping) {
    if (!map.isDirectory) {
      continue;
    }

    const QString src = QDir::cleanPath(QDir::fromNativeSeparators(map.source));
    const QString dst = QDir::cleanPath(QDir::fromNativeSeparators(map.destination));

    if (!(dst == QDir::cleanPath(dataDir) || dst.startsWith(dataPrefix))) {
      continue;
    }

    if (src == QDir::cleanPath(overwriteDir) || src.startsWith(overPrefix)) {
      continue;
    }

    const std::string srcStd = src.toStdString();
    if (!seen.insert(srcStd).second) {
      continue;
    }

    const QString name = QFileInfo(src).fileName();
    mods.emplace_back(name.toStdString(), srcStd);
  }

  return mods;
}

void setupFuseOps(struct fuse_lowlevel_ops* ops)
{
  std::memset(ops, 0, sizeof(struct fuse_lowlevel_ops));
  ops->lookup  = mo2_lookup;
  ops->getattr = mo2_getattr;
  ops->readdir = mo2_readdir;
  ops->open    = mo2_open;
  ops->read    = mo2_read;
  ops->write   = mo2_write;
  ops->create  = mo2_create;
  ops->rename  = mo2_rename;
  ops->setattr = mo2_setattr;
  ops->unlink  = mo2_unlink;
  ops->mkdir   = mo2_mkdir;
  ops->release = mo2_release;
}

}  // namespace

FuseConnector::FuseConnector(QObject* parent) : QObject(parent)
{
  log::debug("FUSE connector initialized");
}

FuseConnector::~FuseConnector()
{
  unmount();
}

bool FuseConnector::mount(
    const QString& mount_point, const QString& overwrite_dir, const QString& game_dir,
    const QString& data_dir_name,
    const std::vector<std::pair<std::string, std::string>>& mods)
{
  if (m_mounted) {
    unmount();
  }

  m_overwriteDir = overwrite_dir.toStdString();
  m_gameDir      = game_dir.toStdString();
  m_dataDirName  = data_dir_name.toStdString();
  m_lastMods     = mods;

  // Use the caller-supplied data directory path directly.  Re-computing it
  // as gameDir/dataDirName breaks games where the data directory IS the game
  // directory (e.g. BG3 with GameDataPath=""), because dirName() returns the
  // last path component and appending it produces a non-existent double path.
  m_dataDirPath = mount_point.toStdString();
  m_mountPoint  = m_dataDirPath;

  if (!fs::exists(m_dataDirPath)) {
    throw FuseConnectorException(
        QObject::tr("Game data directory does not exist: %1")
            .arg(QString::fromStdString(m_dataDirPath)));
  }

  tryCleanupStaleMount(QString::fromStdString(m_mountPoint));

  if (isFlatpak()) {
    return mountViaHelper(overwrite_dir, game_dir, data_dir_name, mods);
  }

  const fs::path overwritePath(m_overwriteDir);
  m_stagingDir = (overwritePath.parent_path() / "VFS_staging").string();

  std::error_code ec;
  fs::create_directories(m_stagingDir, ec);
  fs::create_directories(m_overwriteDir, ec);

  // Scan + cache base game files BEFORE mounting (after mount they're hidden).
  // Reuse the cache across mount/unmount cycles since base game files don't
  // change between runs — this avoids a full recursive directory walk on
  // every launch.
  if (m_baseFileCache.empty() || m_dataDirPath != m_cachedDataDirPath) {
    m_baseFileCache    = scanDataDir(m_dataDirPath);
    m_cachedDataDirPath = m_dataDirPath;
    log::debug("Scanned {} base game entries from {}", m_baseFileCache.size(),
               QString::fromStdString(m_dataDirPath));
  } else {
    log::debug("Reusing cached {} base game entries for {}",
               m_baseFileCache.size(), QString::fromStdString(m_dataDirPath));
  }

  // Open fd to data dir BEFORE mounting so we can access original files
  m_backingFd = open(m_dataDirPath.c_str(), O_RDONLY | O_DIRECTORY);
  if (m_backingFd < 0) {
    throw FuseConnectorException(
        QObject::tr("Failed to open backing fd for %1")
            .arg(QString::fromStdString(m_dataDirPath)));
  }

  // Build tree using cached base files + mods + overwrite
  auto tree = std::make_shared<VfsTree>(
      buildDataDirVfs(m_baseFileCache, m_dataDirPath, mods, m_overwriteDir));

  // Inject file-level data-dir mappings (e.g. plugins.txt, loadorder.txt)
  injectExtraFiles(*tree, m_extraVfsFiles);

  m_context                 = std::make_shared<Mo2FsContext>();
  m_context->tree           = tree;
  m_context->inodes         = std::make_unique<InodeTable>();
  m_context->overwrite      = std::make_unique<OverwriteManager>(m_stagingDir, m_overwriteDir);
  m_context->backing_dir_fd = m_backingFd;
  m_context->uid            = ::getuid();
  m_context->gid            = ::getgid();

  // NOTE: Do NOT include mount_point here — low-level API passes it
  // separately to fuse_session_mount(). Including it here causes
  // "fuse: unknown option(s)" error.
  std::vector<std::string> argvStorage = {
      "mo2fuse", "-o", "fsname=mo2linux", "-o", "default_permissions",
      "-o",      "noatime"};

  std::vector<char*> argv;
  argv.reserve(argvStorage.size());
  for (auto& s : argvStorage) {
    argv.push_back(s.data());
  }

  struct fuse_args args = FUSE_ARGS_INIT(static_cast<int>(argv.size()), argv.data());

  struct fuse_lowlevel_ops ops;
  setupFuseOps(&ops);

  m_session = fuse_session_new(&args, &ops, sizeof(ops), m_context.get());
  if (m_session == nullptr) {
    close(m_backingFd);
    m_backingFd = -1;
    throw FuseConnectorException(QObject::tr("Failed to create FUSE session"));
  }

  if (fuse_session_mount(m_session, m_mountPoint.c_str()) != 0) {
    fuse_session_destroy(m_session);
    m_session = nullptr;
    close(m_backingFd);
    m_backingFd = -1;
    throw FuseConnectorException(
        QObject::tr("Failed to mount FUSE at %1")
            .arg(QString::fromStdString(m_mountPoint)));
  }

  m_fuseThread = std::thread([this]() {
    fuse_session_loop_mt(m_session, nullptr);
  });

  m_mounted = true;
  setFuseMountPointForCrashCleanup(m_mountPoint.c_str());
  log::debug("FUSE mounted on data dir {}", QString::fromStdString(m_mountPoint));
  return true;
}

void FuseConnector::unmount()
{
  if (!m_mounted) {
    return;
  }

  if (m_helperProcess) {
    sendHelperCommand(m_helperProcess, "quit", 10000);
    m_helperProcess->waitForFinished(5000);
    if (m_helperProcess->state() != QProcess::NotRunning) {
      m_helperProcess->kill();
      m_helperProcess->waitForFinished(2000);
    }
    delete m_helperProcess;
    m_helperProcess = nullptr;
    m_mounted       = false;
    setFuseMountPointForCrashCleanup(nullptr);
    cleanupExternalMappings();
    log::debug("VFS helper stopped, FUSE unmounted from {}",
               QString::fromStdString(m_mountPoint));
    return;
  }

  if (m_session != nullptr) {
    fuse_session_exit(m_session);
    fuse_session_unmount(m_session);
  }

  if (m_fuseThread.joinable()) {
    m_fuseThread.join();
  }

  if (m_session != nullptr) {
    fuse_session_destroy(m_session);
    m_session = nullptr;
  }

  flushStaging();

  if (m_backingFd >= 0) {
    close(m_backingFd);
    m_backingFd = -1;
  }

  m_context.reset();
  m_mounted = false;
  setFuseMountPointForCrashCleanup(nullptr);

  // Clean up symlinks created for non-data-dir mappings.
  cleanupExternalMappings();

  log::debug("FUSE unmounted from {}", QString::fromStdString(m_mountPoint));
}

bool FuseConnector::isMounted() const
{
  return m_mounted;
}

void FuseConnector::rebuild(
    const std::vector<std::pair<std::string, std::string>>& mods,
    const QString& overwrite_dir, const QString& data_dir_name)
{
  if (!m_mounted) {
    return;
  }

  m_overwriteDir = overwrite_dir.toStdString();
  m_dataDirName  = data_dir_name.toStdString();
  m_lastMods     = mods;

  if (m_helperProcess) {
    const QString dataDir =
        QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
    const QString configPath = QDir(dataDir).filePath("fluorine/vfs.cfg");
    writeVfsConfig(configPath, QString::fromStdString(m_mountPoint),
                   overwrite_dir, QString::fromStdString(m_gameDir),
                   data_dir_name, mods);
    sendHelperCommand(m_helperProcess, "rebuild", 10000);
    return;
  }

  if (m_context == nullptr) {
    return;
  }

  // Use cached base files - can't re-scan the data dir since it's behind our mount
  auto newTree = std::make_shared<VfsTree>(
      buildDataDirVfs(m_baseFileCache, m_dataDirPath, mods, m_overwriteDir));

  // Inject file-level data-dir mappings (e.g. plugins.txt, loadorder.txt)
  injectExtraFiles(*newTree, m_extraVfsFiles);

  std::unique_lock lock(m_context->tree_mutex);
  m_context->tree.swap(newTree);
}

void FuseConnector::updateMapping(const MappingType& mapping)
{
  auto* game = qApp->property("managed_game").value<MOBase::IPluginGame*>();
  if (game == nullptr) {
    throw FuseConnectorException(QObject::tr("Managed game not available"));
  }

  const QString gameDir      = game->gameDirectory().absolutePath();
  const QString dataDirPath  = game->dataDirectory().absolutePath();
  const QString dataDirName  = game->dataDirectory().dirName();
  const QString overwriteDir = Settings::instance().paths().overwrite();

  auto mods = buildModsFromMapping(mapping, dataDirPath, overwriteDir);

  // Deploy non-data-dir mappings as real symlinks and collect file-level
  // data-dir mappings for VFS tree injection.
  deployExternalMappings(mapping, dataDirPath);

  if (!m_mounted) {
    mount(dataDirPath, overwriteDir, gameDir, dataDirName, mods);
  } else {
    rebuild(mods, overwriteDir, dataDirName);
  }
}

void FuseConnector::deployExternalMappings(const MappingType& mapping,
                                            const QString& dataDir)
{
  cleanupExternalMappings();
  m_extraVfsFiles.clear();

  const QString cleanDataDir = QDir::cleanPath(dataDir);
  const QString dataPrefix   = cleanDataDir + QStringLiteral("/");

  for (const auto& map : mapping) {
    const QString src =
        QDir::cleanPath(QDir::fromNativeSeparators(map.source));
    const QString dst =
        QDir::cleanPath(QDir::fromNativeSeparators(map.destination));

    const bool targetsDataDir =
        (dst == cleanDataDir || dst.startsWith(dataPrefix));

    if (targetsDataDir) {
      if (!map.isDirectory) {
        // File-level mapping INTO the data directory (e.g. plugins.txt).
        // FUSE sits on top, so we cannot create a physical symlink there.
        // Record it for injection into the VFS tree instead.
        const QString relPath = dst.startsWith(dataPrefix)
                                    ? dst.mid(dataPrefix.length())
                                    : QFileInfo(src).fileName();
        m_extraVfsFiles.emplace_back(relPath.toStdString(), src.toStdString());
      }
      // Directory-level data-dir mappings are handled by the FUSE VFS.
      continue;
    }

    // Non-data-dir mapping — deploy via real symlinks so the game
    // (running through Proton) can see the files.
    std::error_code ec;

    if (map.isDirectory) {
      const fs::path srcPath(src.toStdString());
      if (!fs::exists(srcPath, ec)) {
        continue;
      }

      for (auto it = fs::recursive_directory_iterator(
               srcPath, fs::directory_options::skip_permission_denied);
           it != fs::recursive_directory_iterator(); ++it) {
        const auto& entry = *it;
        const fs::path rel = fs::relative(entry.path(), srcPath, ec);
        if (ec || rel.empty()) {
          continue;
        }

        const fs::path destPath = fs::path(dst.toStdString()) / rel;
        if (entry.is_directory(ec)) {
          fs::create_directories(destPath, ec);
        } else if (entry.is_regular_file(ec) || entry.is_symlink(ec)) {
          fs::create_directories(destPath.parent_path(), ec);
          if (fs::exists(destPath, ec) && !fs::is_symlink(destPath, ec)) {
            // Never overwrite real game files — only replace our own symlinks.
            continue;
          }
          if (fs::is_symlink(destPath, ec)) {
            fs::remove(destPath, ec);
          }
          fs::create_symlink(entry.path(), destPath, ec);
          if (!ec) {
            m_externalSymlinks.push_back(destPath.string());
          } else {
            log::warn("Failed to symlink {} -> {}: {}",
                      QString::fromStdString(destPath.string()),
                      QString::fromStdString(entry.path().string()),
                      QString::fromStdString(ec.message()));
          }
        }
      }
    } else {
      // Single file symlink.
      const fs::path destPath(dst.toStdString());
      fs::create_directories(destPath.parent_path(), ec);
      if (fs::exists(destPath, ec) && !fs::is_symlink(destPath, ec)) {
        continue;
      }
      if (fs::is_symlink(destPath, ec)) {
        fs::remove(destPath, ec);
      }
      fs::create_symlink(fs::path(src.toStdString()), destPath, ec);
      if (!ec) {
        m_externalSymlinks.push_back(destPath.string());
      } else {
        log::warn("Failed to symlink {} -> {}: {}", dst, src,
                  QString::fromStdString(ec.message()));
      }
    }
  }

  if (!m_externalSymlinks.empty()) {
    log::debug("Deployed {} external symlinks for non-data-dir mappings",
               m_externalSymlinks.size());
  }
  if (!m_extraVfsFiles.empty()) {
    log::debug("Collected {} extra file mappings for VFS injection",
               m_extraVfsFiles.size());
  }
}

void FuseConnector::cleanupExternalMappings()
{
  if (m_externalSymlinks.empty()) {
    return;
  }

  std::error_code ec;
  for (const auto& path : m_externalSymlinks) {
    if (fs::is_symlink(path, ec)) {
      fs::remove(path, ec);
    }
  }

  log::debug("Cleaned up {} external symlinks", m_externalSymlinks.size());
  m_externalSymlinks.clear();
}

void FuseConnector::updateParams(MOBase::log::Levels /*logLevel*/,
                                 env::CoreDumpTypes /*coreDumpType*/,
                                 const QString& /*crashDumpsPath*/,
                                 std::chrono::seconds /*spawnDelay*/,
                                 QString /*executableBlacklist*/,
                                 const QStringList& /*skipFileSuffixes*/,
                                 const QStringList& /*skipDirectories*/)
{}

void FuseConnector::updateForcedLibraries(
    const QList<MOBase::ExecutableForcedLoadSetting>& /*forced*/)
{}

void FuseConnector::flushStaging()
{
  if (m_stagingDir.empty() || m_overwriteDir.empty()) {
    return;
  }

  const fs::path staging(m_stagingDir);
  const fs::path overwrite(m_overwriteDir);
  if (!fs::exists(staging)) {
    return;
  }

  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(
           staging, fs::directory_options::skip_permission_denied);
       it != fs::recursive_directory_iterator(); ++it) {
    const auto& entry = *it;
    const fs::path rel = fs::relative(entry.path(), staging, ec);
    if (ec || rel.empty()) {
      continue;
    }

    const fs::path dest = overwrite / rel;
    if (entry.is_directory(ec)) {
      fs::create_directories(dest, ec);
      continue;
    }

    if (!entry.is_regular_file(ec)) {
      continue;
    }

    fs::create_directories(dest.parent_path(), ec);
    fs::rename(entry.path(), dest, ec);
    if (ec) {
      ec.clear();
      fs::copy_file(entry.path(), dest, fs::copy_options::overwrite_existing, ec);
      if (!ec) {
        fs::remove(entry.path(), ec);
      }
    }
  }

  fs::remove_all(staging, ec);
}

void FuseConnector::flushStagingLive()
{
  if (!m_mounted) {
    return;
  }

  if (m_helperProcess) {
    sendHelperCommand(m_helperProcess, "flush", 30000);
    return;
  }

  if (m_context == nullptr) {
    return;
  }

  // Move staged files to overwrite
  flushStaging();

  // Re-create the staging dir (flushStaging removes it)
  std::error_code ec;
  fs::create_directories(m_stagingDir, ec);

  // Rebuild the VFS tree to pick up new overwrite files
  auto newTree = std::make_shared<VfsTree>(
      buildDataDirVfs(m_baseFileCache, m_dataDirPath, m_lastMods, m_overwriteDir));

  {
    std::unique_lock lock(m_context->tree_mutex);
    m_context->tree.swap(newTree);
  }

  // Re-create OverwriteManager with fresh staging dir
  m_context->overwrite = std::make_unique<OverwriteManager>(m_stagingDir, m_overwriteDir);

  log::debug("Live staging flush complete");
}

// Detect a stale FUSE mount by probing with stat().  Returns true if
// the path exists in the mount table OR if accessing it gives ENOTCONN
// (which happens when the FUSE daemon died but the mount is listed
// under a different path due to symlinks).
static bool isStaleOrMounted(const QString& path)
{
  if (isMountPoint(path)) {
    return true;
  }

  // Probe the path directly — ENOTCONN means dead FUSE mount even if
  // /proc/mounts lists it under a different (canonical) path.
  struct stat st;
  if (::stat(path.toLocal8Bit().constData(), &st) != 0 && errno == ENOTCONN) {
    return true;
  }

  return false;
}

static void doUnmount(const QString& path)
{
  const QString clean = QDir::cleanPath(path);

  if (runUnmountCommand("fusermount3", {"-u", clean}) ||
      runUnmountCommand("fusermount", {"-u", clean})) {
    log::info("stale mount at '{}' cleaned up successfully", path);
    return;
  }

  // Graceful unmount failed — try force/lazy variants.
  runUnmountCommand("umount", {clean});
  runUnmountCommand("umount", {"-l", clean});
  runUnmountCommand("fusermount3", {"-uz", clean});
  runUnmountCommand("fusermount", {"-uz", clean});

  if (!isStaleOrMounted(path)) {
    log::info("stale mount at '{}' cleaned up (lazy unmount)", path);
  } else {
    log::error("failed to clean up stale mount at '{}'", path);
  }
}

void FuseConnector::tryCleanupStaleMount(const QString& path)
{
  if (!isStaleOrMounted(path)) {
    return;
  }

  log::warn("stale FUSE mount detected at '{}', attempting cleanup", path);
  doUnmount(path);
}

bool FuseConnector::mountViaHelper(
    const QString& overwrite_dir, const QString& game_dir,
    const QString& data_dir_name,
    const std::vector<std::pair<std::string, std::string>>& mods)
{
  const QString dataDir =
      QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
  const QString configPath = QDir(dataDir).filePath("fluorine/vfs.cfg");
  const QString helperBin =
      QDir(dataDir).filePath("fluorine/bin/mo2-vfs-helper");

  if (!QFile::exists(helperBin)) {
    throw FuseConnectorException(
        QObject::tr("VFS helper not found: %1").arg(helperBin));
  }

  writeVfsConfig(configPath, QString::fromStdString(m_mountPoint), overwrite_dir,
                 game_dir, data_dir_name, mods);

  m_helperProcess = new QProcess(this);
  m_helperProcess->setProcessChannelMode(QProcess::SeparateChannels);
  m_helperProcess->start(QStringLiteral("flatpak-spawn"),
                         {QStringLiteral("--host"), helperBin, configPath});

  if (!m_helperProcess->waitForStarted(5000)) {
    const QString err = QString::fromUtf8(m_helperProcess->readAllStandardError());
    delete m_helperProcess;
    m_helperProcess = nullptr;
    throw FuseConnectorException(
        QObject::tr("Failed to start VFS helper process. %1").arg(err));
  }

  if (!waitForHelperLine(m_helperProcess, "mounted", 10000)) {
    const QString err = QString::fromUtf8(m_helperProcess->readAllStandardError());
    const QString out = QString::fromUtf8(m_helperProcess->readAllStandardOutput());
    log::error("VFS helper stderr: {}", err);
    log::error("VFS helper stdout: {}", out);
    m_helperProcess->kill();
    m_helperProcess->waitForFinished(2000);
    delete m_helperProcess;
    m_helperProcess = nullptr;
    throw FuseConnectorException(
        QObject::tr("VFS helper failed to mount FUSE. %1").arg(err));
  }

  m_mounted = true;
  setFuseMountPointForCrashCleanup(m_mountPoint.c_str());
  log::debug("FUSE mounted via helper on {}",
             QString::fromStdString(m_mountPoint));
  return true;
}

void FuseConnector::writeVfsConfig(
    const QString& configPath, const QString& mount_point,
    const QString& overwrite_dir, const QString& game_dir,
    const QString& data_dir_name,
    const std::vector<std::pair<std::string, std::string>>& mods)
{
  QDir().mkpath(QFileInfo(configPath).absolutePath());

  QFile file(configPath);
  if (!file.open(QIODevice::WriteOnly | QIODevice::Text)) {
    throw FuseConnectorException(
        QObject::tr("Failed to write VFS config: %1").arg(configPath));
  }

  QTextStream out(&file);
  out << "mount_point=" << mount_point << "\n";
  out << "game_dir=" << game_dir << "\n";
  out << "data_dir_name=" << data_dir_name << "\n";
  out << "overwrite_dir=" << overwrite_dir << "\n";

  for (const auto& [name, path] : mods) {
    out << "mod=" << QString::fromStdString(name) << "|"
        << QString::fromStdString(path) << "\n";
  }

  for (const auto& [relPath, realPath] : m_extraVfsFiles) {
    out << "extra_file=" << QString::fromStdString(relPath) << "|"
        << QString::fromStdString(realPath) << "\n";
  }
}
