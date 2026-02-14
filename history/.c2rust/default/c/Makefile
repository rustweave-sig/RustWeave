calc: main.c
	gcc -fPIC -rdynamic \
	    -Wl,--unresolved-symbols=ignore-in-object-files \
	    main.c -o calc

clean:
	rm -f calc
	
test: calc
	./calc

build: calc