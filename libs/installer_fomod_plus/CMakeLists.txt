# mo2-fomod-plus — vendored from github.com/aglowinthefield/mo2-fomod-plus
# Commit: da6c07ed4bbe235759910c14c2450e1bfe8fe25e (2026-01-27)
#
# Pure C++20 FOMOD installer replacement (no .NET dependency).
# 3 plugin targets: installer, scanner, patch wizard.

include(FetchContent)

# Force pugixml to build as a static library so plugins don't need libpugixml.so at runtime.
# (The global BUILD_SHARED_LIBS is ON for MO2's own archive lib, which would make pugixml shared.)
set(BUILD_SHARED_LIBS_SAVED ${BUILD_SHARED_LIBS})
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)

# Compatibility: sub-CMakeLists call mo2_install_target but Fluorine defines mo2_install_plugin
if (NOT COMMAND mo2_install_target AND COMMAND mo2_install_plugin)
    function(mo2_install_target target)
        mo2_install_plugin(${target})
    endfunction()
endif ()

# Compute include dirs from existing targets for sub-CMakeLists that reference these variables
get_target_property(MO2_UIBASE_INCLUDE_DIRS mo2::uibase INTERFACE_INCLUDE_DIRECTORIES)
foreach(dir ${MO2_UIBASE_INCLUDE_DIRS})
    list(APPEND MO2_UIBASE_INCLUDE_DIRS "${dir}/uibase" "${dir}/uibase/game_features")
endforeach()
get_target_property(MO2_ARCHIVE_INCLUDE_DIRS mo2::archive INTERFACE_INCLUDE_DIRECTORIES)
foreach(dir ${MO2_ARCHIVE_INCLUDE_DIRS})
    list(APPEND MO2_ARCHIVE_INCLUDE_DIRS "${dir}/archive")
endforeach()

add_subdirectory(installer)
add_subdirectory(scanner)
add_subdirectory(patchwizard)

# Restore BUILD_SHARED_LIBS so subsequent targets aren't affected.
set(BUILD_SHARED_LIBS ${BUILD_SHARED_LIBS_SAVED} CACHE BOOL "" FORCE)
