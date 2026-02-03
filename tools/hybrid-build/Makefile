CC = gcc
CFLAGS = -Wall -fPIC -shared
TARGET = libc2rust-hybrid-build.so

all: $(TARGET)

$(TARGET): hook.c
	$(CC) $(CFLAGS) -o $(TARGET) hook.c -ldl

clean:
	rm -f $(TARGET)

.PHONY: all clean
