# Distributed under the OSI-approved BSD 3-Clause License.  See accompanying
# file LICENSE.rst or https://cmake.org/licensing for details.

cmake_minimum_required(VERSION ${CMAKE_VERSION}) # this file comes with cmake

# If CMAKE_DISABLE_SOURCE_CHANGES is set to true and the source directory is an
# existing directory in our source tree, calling file(MAKE_DIRECTORY) on it
# would cause a fatal error, even though it would be a no-op.
if(NOT EXISTS "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/_deps/picotool-src")
  file(MAKE_DIRECTORY "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/_deps/picotool-src")
endif()
file(MAKE_DIRECTORY
  "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/_deps/picotool-build"
  "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/_deps"
  "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/picotool/tmp"
  "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/picotool/src/picotoolBuild-stamp"
  "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/picotool/src"
  "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/picotool/src/picotoolBuild-stamp"
)

set(configSubDirs )
foreach(subDir IN LISTS configSubDirs)
    file(MAKE_DIRECTORY "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/picotool/src/picotoolBuild-stamp/${subDir}")
endforeach()
if(cfgdir)
  file(MAKE_DIRECTORY "C:/MasterarbeitFinal/Iteration 1/Thesis_Iteration1/pico/build/picotool/src/picotoolBuild-stamp${cfgdir}") # cfgdir has leading slash
endif()
