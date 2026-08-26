#include <stdalign.h>
#include <stdarg.h>

#include "sample.h"

int classify(int value) {
  if (value < 0)
    return -1;
  if (value == 0)
    return 0;
  return 1;
}
