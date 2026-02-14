## 对于`c/Makefile`
- 要混合构建起来得改动Makefile的内容，如demo探索中那样。想必不是所有的Makefile都加上了
```sh
-fPIC -rdynamic \
-Wl,--unresolved-symbols=ignore-in-object-files \
```
编译链接选项，来对注释之后的c项目进行处理。 
所以这里是正则表达式的方式来替换`Makefile`里面的命令吗？  
我先这样试着实现 
有更好的方式吗？

## 对于“ 首先翻译全部的类型信息, 基于`bindgen`.”
这个步骤能给点，你们用bindgen的实际例子吗？ 
我之前有用过bindgen，官方的手册上的例子我复刻起来也比较费劲。  
主要是其build.rs文件的编写，里面的各个参数的怎么填写。  
