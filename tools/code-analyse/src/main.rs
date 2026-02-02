use hierr::Error;
use std::path::{Path, PathBuf};
pub type Result<T> = core::result::Result<T, Error>;

mod file;
use file::*;
mod feature;
use feature::*;

trait ToError<T> {
    fn to_err(self) -> Result<T>;
    fn log_err(self, info: &str) -> Result<T>;
}

impl<T, E: core::error::Error> ToError<T> for core::result::Result<T, E> {
    fn to_err(self) -> Result<T> {
        self.map_err(|e| {
            eprintln!("{}", e);
            Error::last()
        })
    }
    fn log_err(self, info: &str) -> Result<T> {
        self.map_err(|e| {
            eprintln!("Error: {} : {info}", e);
            Error::last()
        })
    }
}

fn get_root() -> Result<PathBuf> {
    let mut root = Path::new(".").canonicalize().map_err(|_| Error::last())?;
    while !root.join(".c2rust").is_dir() {
        if !root.pop() {
            return Err(Error::noent());
        }
    }
    Ok(root)
}

fn get_clang() -> String {
    std::env::var("C2RUST_CLANG").unwrap_or("clang".to_string())
}

fn main() -> Result<()> {
    // 定义支持的选项: feature: 需要参数, init/update/merge 不需要参数
    let opts = hiopt::options!["feature:", "init", "update", "merge"];

    // 获取命令行参数（跳过第一个程序名）
    let args: Vec<String> = std::env::args().collect();
    let args_str: Vec<&str> = args.iter().map(|s| s.as_str()).collect();

    let mut feature_name = None;
    let mut init_flag = false;
    let mut update_flag = false;
    let mut merge_flag = false;

    // 遍历选项
    for opt in opts.opt_iter(&args_str[..]) {
        match opt {
            Ok((idx, arg)) => {
                let opt_name = opts[idx].name();
                match opt_name {
                    "feature" => {
                        feature_name = arg.map(|s| s.to_string());
                    }
                    "init" => {
                        init_flag = true;
                    }
                    "update" => {
                        update_flag = true;
                    }
                    "merge" => {
                        merge_flag = true;
                    }
                    _ => unreachable!(),
                }
            }
            Err(err) => {
                eprintln!("Error parsing options: {:?}", err);
                return Err(Error::inval());
            }
        }
    }

    // 检查是否指定了feature
    let feature_name = feature_name.ok_or_else(|| {
        eprintln!("Error: --feature option is required");
        Error::inval()
    })?;

    // 检查操作标志
    let operations = [
        (init_flag, "init"),
        (update_flag, "update"),
        (merge_flag, "merge"),
    ];
    let specified: Vec<_> = operations.iter().filter(|(flag, _)| *flag).collect();
    if specified.len() != 1 {
        eprintln!("Error: exactly one of --init, --update, or --merge must be specified");
        return Err(Error::inval());
    }

    let mut feature = Feature::new(&feature_name)?;
    match specified[0].1 {
        "init" => {
            feature.reinit()?;
        }
        "update" => {
            feature.update()?;
        }
        "merge" => {
            feature.merge()?;
        }
        _ => unreachable!(),
    }

    Ok(())
}
