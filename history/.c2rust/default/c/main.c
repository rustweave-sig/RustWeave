#include <stdio.h>

/* 只声明，不实现 */
int add(int a, int b);

int square(int x) {
    return x * x;
}

int main() {
    printf("square(3) = %d\n", square(3));
    printf("add(3,4) = %d\n", add(3,4));
    return 0;
}
