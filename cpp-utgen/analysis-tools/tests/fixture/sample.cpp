#include <vector>

#include "sample.hpp"

int Counter::clamp(int value) const {
  if (value < 0)
    return 0;
  return value;
}
