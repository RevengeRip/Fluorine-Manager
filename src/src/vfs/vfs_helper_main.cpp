// Standalone VFS helper for Flatpak FUSE support.
// Runs on the host via flatpak-spawn --host, where FUSE works normally.
// Communicates with MO2 GUI via stdin/stdout pipes.

#include "inodetable.h"
#include "mo2filesystem.h"
#include "overwritemanager.h"
#include "vfstree.h"

#include <fuse3/fuse_lowlevel.h>

#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;

struct HelperConfig
{
  std::string mount_point;
  std::string game_dir;
  std::string data_dir_name;
  std::string overwrite_dir;
  std::vector<std::pair<std::string, std::string>> mods;
  std::vector<std::pair<std::string, std::string>> extra_files;
};

static HelperConfig readConfig(const std::string& path)
{
  HelperConfig cfg;
  std::ifstream in(path);
  std::string line;

  while (std::getline(in, line)) {
    if (line.empty() || line[0] == '#') {
      continue;
    }

    const auto eq = line.find('=');
    if (eq == std::string::npos) {
      continue;
    }

    const std::string key = line.substr(0, eq);
    const std::string val = line.substr(eq + 1);

    if (key == "mount_point") {
      cfg.mount_point = val;
    } else if (key == "game_dir") {
      cfg.game_dir = val;
    } else if (key == "data_dir_name") {
      cfg.data_dir_name = val;
    } else if (key == "overwrite_dir") {
      cfg.overwrite_dir = val;
    } else if (key == "mod") {
      const auto pipe = val.find('|');
      if (pipe != std::string::npos) {
        cfg.mods.emplace_back(val.substr(0, pipe), val.substr(pipe + 1));
      }
    } else if (key == "extra_file") {
      const auto pipe = val.find('|');
      if (pipe != std::string::npos) {
        cfg.extra_files.emplace_back(val.substr(0, pipe), val.substr(pipe + 1));
      }
    }
  }

  return cfg;
}

static void tryUnmountStale(const std::string& path)
{
  pid_t pid = fork();
  if (pid == 0) {
    int devnull = open("/dev/null", O_WRONLY);
    if (devnull >= 0) {
      dup2(devnull, STDERR_FILENO);
      close(devnull);
    }
    execlp("fusermount3", "fusermount3", "-u", path.c_str(), nullptr);
    _exit(1);
  }
  if (pid > 0) {
    int status;
    waitpid(pid, &status, 0);
  }
}

static void flushStaging(const std::string& stagingDir,
                         const std::string& overwriteDir)
{
  const fs::path staging(stagingDir);
  const fs::path overwrite(overwriteDir);
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

static void setupFuseOps(struct fuse_lowlevel_ops* ops)
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

static struct fuse_session* g_session = nullptr;

static void signalHandler(int /*sig*/)
{
  if (g_session) {
    fuse_session_exit(g_session);
  }
}

int main(int argc, char* argv[])
{
  if (argc < 2) {
    std::cerr << "Usage: mo2-vfs-helper <config-file>\n";
    return 1;
  }

  const std::string configPath = argv[1];
  auto config                  = readConfig(configPath);

  if (config.mount_point.empty()) {
    std::cout << "error: mount_point not set in config" << std::endl;
    return 1;
  }

  const std::string dataDirPath = config.mount_point;
  const std::string stagingDir =
      (fs::path(config.overwrite_dir).parent_path() / "VFS_staging").string();

  if (!fs::exists(dataDirPath)) {
    std::cout << "error: data directory does not exist: " << dataDirPath
              << std::endl;
    return 1;
  }

  std::error_code ec;
  fs::create_directories(stagingDir, ec);
  fs::create_directories(config.overwrite_dir, ec);

  // Scan base game files BEFORE mounting (after mount they're hidden)
  auto baseFileCache = scanDataDir(dataDirPath);

  // Open fd to data dir BEFORE mounting so we can access original files
  int backingFd = open(dataDirPath.c_str(), O_RDONLY | O_DIRECTORY);
  if (backingFd < 0) {
    std::cout << "error: failed to open backing fd for " << dataDirPath
              << std::endl;
    return 1;
  }

  // Clean up any stale FUSE mount
  tryUnmountStale(dataDirPath);

  // Build VFS tree
  auto tree = std::make_shared<VfsTree>(
      buildDataDirVfs(baseFileCache, dataDirPath, config.mods,
                      config.overwrite_dir));
  injectExtraFiles(*tree, config.extra_files);

  auto context            = std::make_shared<Mo2FsContext>();
  context->tree           = tree;
  context->inodes         = std::make_unique<InodeTable>();
  context->overwrite =
      std::make_unique<OverwriteManager>(stagingDir, config.overwrite_dir);
  context->backing_dir_fd = backingFd;
  context->uid            = ::getuid();
  context->gid            = ::getgid();

  // Setup FUSE
  std::vector<std::string> argvStorage = {
      "mo2-vfs-helper", "-o", "fsname=mo2linux", "-o", "default_permissions",
      "-o",             "noatime"};

  std::vector<char*> fuseArgv;
  fuseArgv.reserve(argvStorage.size());
  for (auto& s : argvStorage) {
    fuseArgv.push_back(s.data());
  }

  struct fuse_args args =
      FUSE_ARGS_INIT(static_cast<int>(fuseArgv.size()), fuseArgv.data());

  struct fuse_lowlevel_ops ops;
  setupFuseOps(&ops);

  struct fuse_session* session =
      fuse_session_new(&args, &ops, sizeof(ops), context.get());
  if (session == nullptr) {
    close(backingFd);
    std::cout << "error: failed to create FUSE session" << std::endl;
    return 1;
  }

  if (fuse_session_mount(session, dataDirPath.c_str()) != 0) {
    fuse_session_destroy(session);
    close(backingFd);
    std::cout << "error: failed to mount FUSE at " << dataDirPath << std::endl;
    return 1;
  }

  g_session = session;

  // Handle signals for clean shutdown
  struct sigaction sa;
  sa.sa_handler = signalHandler;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;
  sigaction(SIGINT, &sa, nullptr);
  sigaction(SIGTERM, &sa, nullptr);

  // Start FUSE event loop in background thread
  std::thread fuseThread([session]() {
    fuse_session_loop_mt(session, nullptr);
  });

  std::cout << "mounted" << std::endl;

  // Command loop: read commands from stdin
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line == "rebuild") {
      auto newConfig = readConfig(configPath);
      auto newTree   = std::make_shared<VfsTree>(buildDataDirVfs(
          baseFileCache, dataDirPath, newConfig.mods, newConfig.overwrite_dir));
      injectExtraFiles(*newTree, newConfig.extra_files);

      {
        std::unique_lock lock(context->tree_mutex);
        context->tree.swap(newTree);
      }

      config = newConfig;
      std::cout << "ok" << std::endl;
    } else if (line == "flush") {
      flushStaging(stagingDir, config.overwrite_dir);
      fs::create_directories(stagingDir, ec);

      auto newTree = std::make_shared<VfsTree>(buildDataDirVfs(
          baseFileCache, dataDirPath, config.mods, config.overwrite_dir));
      injectExtraFiles(*newTree, config.extra_files);

      {
        std::unique_lock lock(context->tree_mutex);
        context->tree.swap(newTree);
      }

      context->overwrite =
          std::make_unique<OverwriteManager>(stagingDir, config.overwrite_dir);
      std::cout << "ok" << std::endl;
    } else if (line == "quit") {
      break;
    }
  }

  // Clean shutdown
  fuse_session_exit(session);
  fuse_session_unmount(session);

  if (fuseThread.joinable()) {
    fuseThread.join();
  }

  fuse_session_destroy(session);
  g_session = nullptr;

  flushStaging(stagingDir, config.overwrite_dir);
  close(backingFd);

  std::cout << "ok" << std::endl;
  return 0;
}
