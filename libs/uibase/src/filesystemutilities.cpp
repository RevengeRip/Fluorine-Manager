#include <uibase/filesystemutilities.h>

#include <QDir>
#include <QFileInfo>
#include <QRegularExpression>
#include <QString>

namespace MOBase
{

bool fixDirectoryName(QString& name)
{
  QString temp = name.simplified();
  while (temp.endsWith('.'))
    temp.chop(1);

  temp.replace(QRegularExpression(R"([<>:"/\\|?*])"), "");
  static QString invalidNames[] = {"CON",  "PRN",  "AUX",  "NUL",  "COM1", "COM2",
                                   "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
                                   "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
                                   "LPT6", "LPT7", "LPT8", "LPT9"};
  for (unsigned int i = 0; i < sizeof(invalidNames) / sizeof(QString); ++i) {
    if (temp == invalidNames[i]) {
      temp = "";
      break;
    }
  }

  temp = temp.simplified();

  if (temp.length() >= 1) {
    name = temp;
    return true;
  } else {
    return false;
  }
}

QString sanitizeFileName(const QString& name, const QString& replacement)
{
  QString new_name = name;

  // Remove characters not allowed by Windows
  new_name.replace(QRegularExpression("[\\x{00}-\\x{1f}\\\\/:\\*\\?\"<>|]"),
                   replacement);

  // Don't end with a period or a space
  // Don't be "." or ".."
  new_name.remove(QRegularExpression("[\\. ]*$"));

  // Recurse until stuff stops changing
  if (new_name != name) {
    return sanitizeFileName(new_name);
  }
  return new_name;
}

bool validFileName(const QString& name)
{
  if (name.isEmpty()) {
    return false;
  }
  if (name == "." || name == "..") {
    return false;
  }

  return (name == sanitizeFileName(name));
}

QString resolveFileCaseInsensitive(const QString& path)
{
#ifdef _WIN32
  return QDir::cleanPath(path);
#else
  const QFileInfo info(path);
  if (info.exists()) {
    return info.absoluteFilePath();
  }

  QDir dir(info.path());
  if (!dir.exists()) {
    return QDir::cleanPath(path);
  }

  const QString target = info.fileName();
  const QStringList entries =
      dir.entryList(QDir::Files | QDir::Readable | QDir::Hidden | QDir::System);
  for (const QString& entry : entries) {
    if (entry.compare(target, Qt::CaseInsensitive) == 0) {
      return dir.absoluteFilePath(entry);
    }
  }

  return QDir::cleanPath(path);
#endif
}

}  // namespace MOBase
