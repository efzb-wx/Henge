#include "../devices/metallib.h"

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif

EXPORT int henge_test_relabel(uint8_t *data, size_t size, int os, int stage, const char **why) {
    return hv_metallib_relabel_for(data, size, stage,
                                  os == 16 ? &hv_metallib_ios16 : &hv_metallib_ios17, why);
}
