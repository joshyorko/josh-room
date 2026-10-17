//! Link against the candidate's actual release libraries, without changing its graph.
use std::{collections::BTreeMap, env, path::PathBuf};
use rustic_backend::OpenDALBackend;
use rustic_core::{FileType, Id, ReadBackend};

fn main() {
    let root = PathBuf::from(env::var("JOSH_ROOM_CANDIDATE_TLS_FIXTURE").unwrap());
    let endpoint = env::var("JOSH_ROOM_CANDIDATE_TLS_ENDPOINT").unwrap();
    let base = BTreeMap::from([
        ("endpoint".to_string(), endpoint),
        ("bucket".to_string(), "synthetic-candidate".to_string()),
        ("root".to_string(), "repository".to_string()),
        ("region".to_string(), "us-east-1".to_string()),
        ("access_key_id".to_string(), "synthetic-key".to_string()),
        ("secret_access_key".to_string(), "synthetic-secret".to_string()),
        ("retry".to_string(), "off".to_string()),
    ]);
    for name in ["empty", "malformed", "invalid-der", "nested-section", "trailing-junk"] {
        let mut options = base.clone();
        options.insert("cacert".to_string(), root.join(format!("{name}.pem")).to_str().unwrap().to_string());
        assert!(OpenDALBackend::new("s3", options).is_err(), "invalid CA admitted at construction: {name}");
    }
    let mut trusted_options = base.clone();
    trusted_options.insert("cacert".to_string(), root.join("ca.pem").to_str().unwrap().to_string());
    let trusted = OpenDALBackend::new("s3", trusted_options).unwrap();
    assert!(trusted.read_full(FileType::Config, &Id::default()).is_ok());
    let ordinary = OpenDALBackend::new("s3", base.clone()).unwrap();
    assert!(ordinary.read_full(FileType::Config, &Id::default()).is_err());
    let mut wrong_options = base;
    wrong_options.insert("cacert".to_string(), root.join("other.pem").to_str().unwrap().to_string());
    let wrong = OpenDALBackend::new("s3", wrong_options).unwrap();
    assert!(wrong.read_full(FileType::Config, &Id::default()).is_err());
    assert!(trusted.read_full(FileType::Config, &Id::default()).is_ok());
    println!("SCOPED_OPERATOR_ISOLATION_PASS");
}
